"""验证差异热力图不会混入 /api/results 处理结果列表。

运行：python tests/test_diff_excluded.py
在临时 data 目录中走通：上传 -> 运行流水线 -> 生成差异热力图 -> 校验结果列表，
不污染真实 data/。
"""
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw  # noqa: E402

_tmp = tempfile.mkdtemp(prefix="cvtest-")

from server import config  # noqa: E402

# 重定向所有运行时路径到临时目录（必须在导入 server.api 之前完成）
config.DATA_DIR = _tmp
config.IMAGES_DIR = os.path.join(_tmp, "images")
config.RESULTS_DIR = os.path.join(_tmp, "results")
config.THUMBS_DIR = os.path.join(_tmp, "thumbnails")
config.CACHE_DIR = os.path.join(_tmp, "cache")
config.META_DIR = os.path.join(_tmp, "metadata")
for _name in ("IMAGES_JSON", "PIPELINES_JSON", "HISTORY_JSON",
              "PRESETS_JSON", "QUEUE_JSON", "CACHE_JSON"):
    setattr(config, _name, os.path.join(config.META_DIR, os.path.basename(getattr(config, _name))))
config._ALL_DIRS = [config.DATA_DIR, config.IMAGES_DIR, config.RESULTS_DIR,
                    config.THUMBS_DIR, config.CACHE_DIR, config.META_DIR]
config.ensure_dirs()

from app import create_app  # noqa: E402


def _png_bytes(color=(30, 60, 90)):
    img = Image.new("RGB", (128, 96), color)
    ImageDraw.Draw(img).rectangle([10, 10, 60, 50], fill=(220, 200, 10))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def main():
    client = create_app().test_client()

    # 1) 上传原图
    r = client.post("/api/images", data={"files": (io.BytesIO(_png_bytes()), "t.png")},
                    content_type="multipart/form-data")
    assert r.status_code == 200, r.data
    image_id = r.get_json()["saved"][0]["id"]

    # 2) 跑一条简单流水线，得到真正的处理结果
    nodes = [{"id": "n1", "type": "brightness", "params": {"amount": 40}, "inputs": []}]
    r = client.post("/api/run", json={"image_id": image_id, "nodes": nodes,
                                      "pipeline_name": "验证"})
    run = r.get_json()
    assert "result_id" in run, run
    real_result_id = run["result_id"]

    # 3) 连续生成两次差异热力图（同一组合第二次命中缓存，再换参数生成一张新的）
    r = client.post("/api/compare/diff", json={"image_id": image_id,
                                               "result_id": real_result_id})
    assert r.status_code == 200, r.data
    diff = r.get_json()
    assert diff["kind"] == "diff_heatmap", diff
    diff_id = diff["result_id"]
    assert diff_id != real_result_id

    nodes2 = [{"id": "n1", "type": "brightness", "params": {"amount": -40}, "inputs": []}]
    r = client.post("/api/run", json={"image_id": image_id, "nodes": nodes2})
    other_result_id = r.get_json()["result_id"]
    client.post("/api/compare/diff", json={"image_id": image_id,
                                           "result_id": other_result_id})

    # 4) /api/results 只应包含处理结果，差异热力图必须被排除
    r = client.get("/api/results")
    results = r.get_json()["results"]
    ids = [x["result_id"] for x in results]
    kinds = {x["result_id"]: x["kind"] for x in results}
    assert real_result_id in ids, "真实处理结果不应被过滤"
    assert other_result_id in ids, "真实处理结果不应被过滤"
    assert all(k == "result" for k in kinds.values()), kinds
    entries = client.get("/api/health").get_json()["results"]
    assert all(i not in ids for i in (diff_id,)), "差异热力图混入了结果列表"
    assert entries >= len(ids) + 1, "差异热力图应仍保留在缓存中，只是不列出"

    # 5) 热力图文件仍可通过其 result_id 直接访问（对比页展示依赖它）
    r = client.get(f"/api/results/{diff_id}/file")
    assert r.status_code == 200 and r.mimetype == "image/png", (r.status_code, r.mimetype)

    print(f"  处理结果数: {len(ids)}；缓存总条目: {entries}（含差异热力图）")
    print("差异热力图已从结果列表排除，且文件仍可访问 ✔")

    # 6) 模拟升级前的旧缓存（无 kind 字段、旧式 diff 键），验证启动时一次性迁移
    from server import api as api_mod
    from server.api import DIFF_HEATMAP_KIND
    from server.cache import make_key

    image_hash = api_mod.image_store.get(image_id)["hash"]
    legacy_key = make_key(image_hash, real_result_id, "diff")
    # 旧式条目：直接以旧键 put 但不标 kind（模拟旧版本写入）
    legacy_heat = Image.new("RGB", (128, 96), (255, 0, 0))
    legacy_id = _put_legacy_entry(legacy_key, legacy_heat)
    listed_before = {x["result_id"] for x in client.get("/api/results").get_json()["results"]}
    assert legacy_id in listed_before, "前置条件：旧条目未标记时应被当作普通结果"

    api_mod._mark_legacy_diff_results()
    listed_after = {x["result_id"] for x in client.get("/api/results").get_json()["results"]}
    assert legacy_id not in listed_after, "旧差异热力图应在迁移后被排除"
    entry = api_mod.cache.get_entry(legacy_id)
    assert entry and entry["kind"] == DIFF_HEATMAP_KIND
    print("升级前残留的旧差异热力图也已被启动迁移补标并排除 ✔")


def _put_legacy_entry(key, image):
    """绕过新 kind 参数，按旧版结构写入一条缓存条目。"""
    import time
    import uuid
    from server.algorithms import util as algo_util
    from server.storage import JsonStore, now_iso

    result_id = uuid.uuid4().hex
    file_name = result_id + ".png"
    dest = os.path.join(config.RESULTS_DIR, file_name)
    rgb = algo_util.ensure_rgb(image)
    rgb.save(dest, "PNG")
    store = JsonStore(config.CACHE_JSON, {})
    doc = store.read()
    doc[key] = {
        "result_id": result_id, "key": key, "file": file_name,
        "size_bytes": os.path.getsize(dest),
        "width": rgb.size[0], "height": rgb.size[1], "meta": {},
        "created_at": now_iso(), "last_access": time.time(),
    }
    store.write(doc)
    return result_id


if __name__ == "__main__":
    main()
