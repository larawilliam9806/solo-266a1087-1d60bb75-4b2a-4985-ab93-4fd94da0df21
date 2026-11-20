"""FastAPI 入口：版本化发布、整批替换、扩区拼接、同版一致性查询。

并发与原子性：
- 重活（几何核查、井归属重算）在写事务之外基于 base 版本快照完成；
- 写事务用 BEGIN IMMEDIATE 串行化，进入后先核对 meta.current_revision
  仍等于 base_revision，不等则回滚返回 409 —— 同版并发修订只成功一份；
- 版本行、区域行、宗地行、井归属行、版本指针在同一个事务里提交，
  失败/重启不留半版；扩区的新范围、新宗地、新井与重算归属同版生效。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .db import connect, init_db
from .geo import (attribute_wells, parse_point, parse_polygon,
                  polygon_to_geojson, validate_expansion, validate_tiling)
from .schemas import MAX_PARCELS, MAX_WELLS, ExpandRequest, InitRequest, ReplaceRequest


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="宗地边界与地下水采样井归属发布 API",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------- 工具

def _err(status: int, payload: dict) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": payload})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _current_revision(conn: sqlite3.Connection) -> Optional[int]:
    row = conn.execute("SELECT current_revision FROM meta WHERE id = 1").fetchone()
    return int(row[0]) if row is not None else None


def _load_region(conn: sqlite3.Connection, revision: int) -> dict:
    """指定版本当时的监测范围（区域随扩区演进，按版本存取）。"""
    row = conn.execute(
        "SELECT geojson FROM region_versions WHERE revision = ?",
        (revision,),
    ).fetchone()
    return json.loads(row[0])


def _load_parcels(conn: sqlite3.Connection, revision: int) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT parcel_id, geojson FROM parcels WHERE revision = ? ORDER BY parcel_id",
        (revision,),
    ).fetchall()
    return {r[0]: json.loads(r[1]) for r in rows}


def _load_well_geoms(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute("SELECT well_id, geojson FROM wells ORDER BY well_id").fetchall()
    return {r[0]: json.loads(r[1]) for r in rows}


def _load_attributions(conn: sqlite3.Connection, revision: int) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT well_id, status, parcel_id, candidates FROM well_attributions "
        "WHERE revision = ? ORDER BY well_id",
        (revision,),
    ).fetchall()
    return {
        r[0]: {"status": r[1], "parcel_id": r[2], "candidates": json.loads(r[3])}
        for r in rows
    }


def _insert_version(conn: sqlite3.Connection, revision: int,
                    parcels: dict[str, dict], attributions: dict[str, dict],
                    *, region: Optional[dict], wells: Optional[dict[str, dict]]) -> None:
    """在已开启的事务内写入一个完整版本（不含 meta 指针）。

    region 为该版本当时的监测范围；wells 为本次新登记的井（几何不可变，
    只增不改）；该版本有哪些井由 attributions 的行集决定。
    """
    conn.execute(
        "INSERT INTO versions (revision, created_at, parcel_count, well_count) "
        "VALUES (?, ?, ?, ?)",
        (revision, _now(), len(parcels), len(attributions)),
    )
    if region is not None:
        conn.execute(
            "INSERT INTO region_versions (revision, geojson) VALUES (?, ?)",
            (revision, json.dumps(region)),
        )
    if wells:
        conn.executemany(
            "INSERT INTO wells (well_id, geojson) VALUES (?, ?)",
            [(wid, json.dumps(g)) for wid, g in wells.items()],
        )
    conn.executemany(
        "INSERT INTO parcels (revision, parcel_id, geojson) VALUES (?, ?, ?)",
        [(revision, pid, json.dumps(g)) for pid, g in parcels.items()],
    )
    conn.executemany(
        "INSERT INTO well_attributions (revision, well_id, status, parcel_id, candidates) "
        "VALUES (?, ?, ?, ?, ?)",
        [(revision, wid, a["status"], a["parcel_id"], json.dumps(a["candidates"]))
         for wid, a in attributions.items()],
    )


def _validation_error(reasons: list[dict], *, gap_area: Optional[float] = None,
                      overlaps: Optional[list] = None,
                      overlap_parcel_ids: Optional[list] = None) -> JSONResponse:
    return _err(422, {
        "error": "validation_failed",
        "message": "整批新边界未通过核查，当前版本保持不变",
        "gap_area": gap_area,
        "overlaps": overlaps or [],
        "overlap_parcel_ids": overlap_parcel_ids or [],
        "reasons": reasons,
    })


def _well_summary(attributions: dict[str, dict]) -> dict:
    summary = {"assigned": 0, "pending": 0, "uncovered": 0}
    for a in attributions.values():
        summary[a["status"]] += 1
    return summary


def _parse_parcels(items, reasons: list[dict],
                   existing: Optional[dict] = None,
                   ) -> tuple[dict[str, Any], dict[str, dict]]:
    """解析一批宗地几何，把重复编号与几何错误追加到 reasons。

    existing 给出当前已存在的宗地（id -> geojson）：扩区时新宗地编号
    与现有编号冲突也算 duplicate_parcel_id（合并后同类编号不得重复）。
    返回 (id -> shapely Polygon, id -> 原始 GeoJSON)。
    """
    geoms: dict[str, Any] = {}
    geojson_by_id: dict[str, dict] = {}
    for p in items:
        if p.id in geojson_by_id:
            reasons.append({"code": "duplicate_parcel_id", "parcel_id": p.id,
                            "message": f"宗地编号 {p.id} 在请求中重复"})
            continue
        if existing is not None and p.id in existing:
            reasons.append({"code": "duplicate_parcel_id", "parcel_id": p.id,
                            "message": f"宗地编号 {p.id} 与现有宗地重复，"
                                       "合并后同类编号不得重复"})
            continue
        geom, errs = parse_polygon(p.geometry, parcel_id=p.id, label=f"宗地 {p.id}")
        reasons.extend(errs)
        if geom is not None:
            geoms[p.id] = geom
            geojson_by_id[p.id] = p.geometry
    return geoms, geojson_by_id


def _parse_wells(items, reasons: list[dict],
                 existing: Optional[dict] = None,
                 ) -> tuple[dict[str, Any], dict[str, dict]]:
    """解析一批井几何，规则同 _parse_parcels。返回 (id -> Point, id -> GeoJSON)。"""
    geoms: dict[str, Any] = {}
    geojson_by_id: dict[str, dict] = {}
    for w in items:
        if w.id in geojson_by_id:
            reasons.append({"code": "duplicate_well_id", "well_id": w.id,
                            "message": f"井编号 {w.id} 在请求中重复"})
            continue
        if existing is not None and w.id in existing:
            reasons.append({"code": "duplicate_well_id", "well_id": w.id,
                            "message": f"井编号 {w.id} 与现有井重复，"
                                       "合并后同类编号不得重复"})
            continue
        geom, errs = parse_point(w.geometry, well_id=w.id, label=f"井 {w.id}")
        reasons.extend(errs)
        if geom is not None:
            geoms[w.id] = geom
            geojson_by_id[w.id] = w.geometry
    return geoms, geojson_by_id


# ---------------------------------------------------------------- 首次提交

@app.post("/v1/init", status_code=201)
def initialize(req: InitRequest):
    reasons: list[dict] = []

    region_geom, errs = parse_polygon(req.region, label="区域")
    reasons.extend(errs)

    parcel_geoms, parcel_gj = _parse_parcels(req.parcels, reasons)
    well_geoms, well_gj = _parse_wells(req.wells, reasons)

    tiling = None
    if region_geom is not None and len(parcel_geoms) == len(req.parcels):
        tiling = validate_tiling(parcel_geoms, region_geom)
        reasons.extend(tiling.reasons)
    if reasons:
        return _validation_error(
            reasons,
            gap_area=tiling.gap_area if tiling else None,
            overlaps=tiling.overlaps if tiling else None,
            overlap_parcel_ids=tiling.overlap_parcel_ids if tiling else None,
        )

    attributions = attribute_wells(well_geoms, parcel_geoms)

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if _current_revision(conn) is not None:
                conn.execute("ROLLBACK")
                return _err(409, {"error": "already_initialized",
                                  "message": "数据集已完成首次提交，不能重复初始化"})
            _insert_version(conn, 1, parcel_gj, attributions,
                            region=req.region, wells=well_gj)
            # legacy 初始区域记录：仅作为旧库迁移回填的来源保留。
            conn.execute("INSERT INTO regions (id, geojson) VALUES (1, ?)",
                         (json.dumps(req.region),))
            conn.execute("INSERT INTO meta (id, current_revision) VALUES (1, 1)")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return {"revision": 1, "parcel_count": len(parcel_gj),
            "well_count": len(well_gj), "well_summary": _well_summary(attributions)}


# ---------------------------------------------------------------- 整批替换

@app.put("/v1/parcels")
def replace_parcels(req: ReplaceRequest):
    # 1) 读取 base 版本快照（读事务，不阻塞他人）
    conn = connect()
    try:
        current = _current_revision(conn)
        if current is None:
            return _err(404, {"error": "not_initialized",
                              "message": "尚未首次提交，请先调用 POST /v1/init"})
        if req.base_revision != current:
            return _err(409, {
                "error": "revision_conflict",
                "message": f"所见修订号 {req.base_revision} 已过期，当前为 {current}，"
                           "请重新读取后再提交",
                "current_revision": current,
                "base_revision": req.base_revision,
            })
        region_gj = _load_region(conn, current)
        old_parcel_gj = _load_parcels(conn, current)
        well_gj = _load_well_geoms(conn)
    finally:
        conn.close()

    # 2) 应用整批替换：只允许替换当前版本中存在的宗地
    reasons: list[dict] = []
    region_geom, _ = parse_polygon(region_gj, label="区域")
    old_geoms = {}
    for pid, gj in old_parcel_gj.items():
        geom, _ = parse_polygon(gj, parcel_id=pid, label=f"宗地 {pid}")
        old_geoms[pid] = geom

    new_geoms = dict(old_geoms)
    new_parcel_gj = dict(old_parcel_gj)
    seen: set[str] = set()
    for p in req.parcels:
        if p.id in seen:
            reasons.append({"code": "duplicate_parcel_id", "parcel_id": p.id,
                            "message": f"宗地编号 {p.id} 在请求中重复"})
            continue
        seen.add(p.id)
        if p.id not in old_parcel_gj:
            reasons.append({"code": "unknown_parcel_id", "parcel_id": p.id,
                            "message": f"宗地 {p.id} 在当前版本中不存在，"
                                       "只能整批替换已有宗地"})
            continue
        geom, errs = parse_polygon(p.geometry, parcel_id=p.id, label=f"宗地 {p.id}")
        reasons.extend(errs)
        if geom is not None:
            new_geoms[p.id] = geom
            new_parcel_gj[p.id] = p.geometry
    if reasons:
        return _validation_error(reasons)

    # 3) 相邻宗地联改按整批新边界核查
    tiling = validate_tiling(new_geoms, region_geom)
    if not tiling.ok:
        return _validation_error(
            tiling.reasons,
            gap_area=tiling.gap_area,
            overlaps=tiling.overlaps,
            overlap_parcel_ids=tiling.overlap_parcel_ids,
        )

    # 4) 依新版边界重算全部井归属
    well_geoms = {}
    for wid, gj in well_gj.items():
        geom, _ = parse_point(gj, well_id=wid, label=f"井 {wid}")
        well_geoms[wid] = geom
    attributions = attribute_wells(well_geoms, new_geoms)

    # 5) 乐观并发提交：同版并发修订只成功一份
    new_revision = current + 1
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if _current_revision(conn) != req.base_revision:
                conn.execute("ROLLBACK")
                return _err(409, {
                    "error": "revision_conflict",
                    "message": "提交期间已有其他修订发布，本次修订作废，请基于新版重试",
                    "current_revision": _current_revision(conn),
                    "base_revision": req.base_revision,
                })
            _insert_version(conn, new_revision, new_parcel_gj, attributions,
                            region=region_gj, wells=None)
            cur = conn.execute(
                "UPDATE meta SET current_revision = ? WHERE id = 1 AND current_revision = ?",
                (new_revision, req.base_revision),
            )
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                return _err(409, {"error": "revision_conflict",
                                  "message": "版本指针已被其他修订更新",
                                  "base_revision": req.base_revision})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return {"revision": new_revision, "parcel_count": len(new_parcel_gj),
            "well_count": len(well_gj), "well_summary": _well_summary(attributions)}


# ---------------------------------------------------------------- 扩区

@app.post("/v1/expand", status_code=201)
def expand_region(req: ExpandRequest):
    """扩区：邻区边界 + 邻区宗地 + 该区新井，与当前范围拼接后整体发布新版本。

    新范围、合并后的宗地与新井、全量重算的井归属在同一事务里生效；
    校验失败、基准过期或保存出错时本次扩区整批不生效。可多次提交。
    """
    # 1) 读取 base 版本快照（读路径不阻塞他人；最终闸门在写事务里）
    conn = connect()
    try:
        current = _current_revision(conn)
        if current is None:
            return _err(404, {"error": "not_initialized",
                              "message": "尚未首次提交，请先调用 POST /v1/init"})
        if req.base_revision != current:
            return _err(409, {
                "error": "revision_conflict",
                "message": f"所见修订号 {req.base_revision} 已过期，当前为 {current}，"
                           "请重新读取后再提交",
                "current_revision": current,
                "base_revision": req.base_revision,
            })
        region_gj = _load_region(conn, current)
        old_parcel_gj = _load_parcels(conn, current)
        old_well_gj = _load_well_geoms(conn)
    finally:
        conn.close()

    # 2) 解析与核查（重活都在写事务之外基于快照完成）
    reasons: list[dict] = []
    current_geom, _ = parse_polygon(region_gj, label="当前区域")
    neighbor_geom, errs = parse_polygon(req.region, label="邻区")
    reasons.extend(errs)

    new_parcel_geoms, new_parcel_gj = _parse_parcels(req.parcels, reasons,
                                                     existing=old_parcel_gj)
    new_well_geoms, new_well_gj = _parse_wells(req.wells, reasons,
                                               existing=old_well_gj)

    # 合并后总量限制
    total_parcels = len(old_parcel_gj) + len(new_parcel_gj)
    total_wells = len(old_well_gj) + len(new_well_gj)
    if total_parcels > MAX_PARCELS:
        reasons.append({"code": "too_many_parcels",
                        "message": f"合并后宗地共 {total_parcels} 块，"
                                   f"超过上限 {MAX_PARCELS}"})
    if total_wells > MAX_WELLS:
        reasons.append({"code": "too_many_wells",
                        "message": f"合并后井共 {total_wells} 口，超过上限 {MAX_WELLS}"})

    # 邻区与当前区域：共边、内部不重叠、拼接后为无洞单环
    merged_geom = None
    if current_geom is not None and neighbor_geom is not None:
        merged_geom, errs = validate_expansion(current_geom, neighbor_geom)
        reasons.extend(errs)

    # 邻区宗地按原有规则做覆盖核查
    tiling = None
    if neighbor_geom is not None and len(new_parcel_geoms) == len(req.parcels):
        tiling = validate_tiling(new_parcel_geoms, neighbor_geom)
        reasons.extend(tiling.reasons)

    # 新井须落在邻区内或边界上
    if neighbor_geom is not None:
        for wid, pt in new_well_geoms.items():
            if not neighbor_geom.intersects(pt):
                reasons.append({"code": "well_outside_region", "well_id": wid,
                                "message": f"井 {wid} 不在邻区内或邻区边界上"})
    if reasons:
        return _validation_error(
            reasons,
            gap_area=tiling.gap_area if tiling else None,
            overlaps=tiling.overlaps if tiling else None,
            overlap_parcel_ids=tiling.overlap_parcel_ids if tiling else None,
        )

    # 3) 合并集合，按拼接后的宗地全量重算归属（原外沿井同样列全候选）
    old_geoms = {}
    for pid, gj in old_parcel_gj.items():
        geom, _ = parse_polygon(gj, parcel_id=pid, label=f"宗地 {pid}")
        old_geoms[pid] = geom
    old_well_geoms = {}
    for wid, gj in old_well_gj.items():
        geom, _ = parse_point(gj, well_id=wid, label=f"井 {wid}")
        old_well_geoms[wid] = geom

    merged_parcel_gj = {**old_parcel_gj, **new_parcel_gj}
    attributions = attribute_wells({**old_well_geoms, **new_well_geoms},
                                   {**old_geoms, **new_parcel_geoms})
    merged_region_gj = polygon_to_geojson(merged_geom)

    # 4) 乐观并发提交：同版并发扩区/改图只成功一份
    new_revision = current + 1
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if _current_revision(conn) != req.base_revision:
                conn.execute("ROLLBACK")
                return _err(409, {
                    "error": "revision_conflict",
                    "message": "提交期间已有其他修订发布，本次扩区作废，请基于新版重试",
                    "current_revision": _current_revision(conn),
                    "base_revision": req.base_revision,
                })
            _insert_version(conn, new_revision, merged_parcel_gj, attributions,
                            region=merged_region_gj, wells=new_well_gj)
            cur = conn.execute(
                "UPDATE meta SET current_revision = ? WHERE id = 1 AND current_revision = ?",
                (new_revision, req.base_revision),
            )
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                return _err(409, {"error": "revision_conflict",
                                  "message": "版本指针已被其他修订更新",
                                  "base_revision": req.base_revision})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return {"revision": new_revision, "parcel_count": len(merged_parcel_gj),
            "well_count": total_wells, "well_summary": _well_summary(attributions)}


# ---------------------------------------------------------------- 查询

def _wells_payload(well_gj: dict[str, dict], attributions: dict[str, dict]) -> list[dict]:
    """该版本的井清单 + 归属。

    井只增不改、几何存一份；某版本"当时有哪些井"由该版归属行集决定，
    因此历史修订不会看到后来扩区才登记的井。
    """
    return [
        {"id": wid, "geometry": well_gj[wid], **attributions[wid]}
        for wid in sorted(attributions)
    ]


@app.get("/v1/current")
def get_current():
    """当前完整版本：区域 + 边界 + 井归属，同一读事务取出，保证同版一致。"""
    conn = connect()
    try:
        conn.execute("BEGIN")
        try:
            revision = _current_revision(conn)
            if revision is None:
                conn.execute("ROLLBACK")
                return _err(404, {"error": "not_initialized",
                                  "message": "尚未首次提交"})
            region = _load_region(conn, revision)
            parcels = _load_parcels(conn, revision)
            well_gj = _load_well_geoms(conn)
            attributions = _load_attributions(conn, revision)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return {
        "revision": revision,
        "region": region,
        "parcels": [{"id": pid, "geometry": parcels[pid]} for pid in sorted(parcels)],
        "wells": _wells_payload(well_gj, attributions),
    }


@app.get("/v1/parcels")
def get_parcels():
    """当前宗地边界（响应携带所属修订号）。"""
    conn = connect()
    try:
        revision = _current_revision(conn)
        if revision is None:
            return _err(404, {"error": "not_initialized", "message": "尚未首次提交"})
        parcels = _load_parcels(conn, revision)
    finally:
        conn.close()
    return {"revision": revision,
            "parcels": [{"id": pid, "geometry": parcels[pid]} for pid in sorted(parcels)]}


@app.get("/v1/wells")
def get_wells():
    """当前井归属（响应携带所属修订号，与 /v1/parcels 对号即可验证同版）。"""
    conn = connect()
    try:
        revision = _current_revision(conn)
        if revision is None:
            return _err(404, {"error": "not_initialized", "message": "尚未首次提交"})
        well_gj = _load_well_geoms(conn)
        attributions = _load_attributions(conn, revision)
    finally:
        conn.close()
    return {"revision": revision, "wells": _wells_payload(well_gj, attributions)}


@app.get("/v1/revisions")
def list_revisions():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT revision, created_at, parcel_count, well_count "
            "FROM versions ORDER BY revision"
        ).fetchall()
    finally:
        conn.close()
    return {"revisions": [dict(r) for r in rows]}


@app.get("/v1/revisions/{revision}")
def get_revision(revision: int):
    """读取指定历史版本（含被替换前的旧版）。"""
    conn = connect()
    try:
        conn.execute("BEGIN")
        try:
            row = conn.execute(
                "SELECT revision, created_at, parcel_count, well_count "
                "FROM versions WHERE revision = ?", (revision,),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return _err(404, {"error": "revision_not_found",
                                  "message": f"版本 {revision} 不存在"})
            region = _load_region(conn, revision)
            parcels = _load_parcels(conn, revision)
            well_gj = _load_well_geoms(conn)
            attributions = _load_attributions(conn, revision)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return {
        "revision": row[0],
        "created_at": row[1],
        "region": region,
        "parcels": [{"id": pid, "geometry": parcels[pid]} for pid in sorted(parcels)],
        "wells": _wells_payload(well_gj, attributions),
    }


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
