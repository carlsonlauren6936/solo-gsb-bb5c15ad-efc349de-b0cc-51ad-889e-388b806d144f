"""锚点建议模块测试:候选枚举、稳定排序、最优单调链选择与只读 API。"""
from __future__ import annotations

import random

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.suggestions import candidate_fragments, select_chain, suggest_anchors


def _frag_map(fragments):
    return {"".join(f.text): (f.starts, f.ends) for f in fragments}


# ---------------------------------------------------------------- 候选枚举

def test_candidate_is_triple_unique_common_fragment():
    # "甲乙" 在三方都唯一出现;"丙"只在两方出现;"丁"在来源 0 出现两次
    seqs = [list("丁甲乙丁"), list("丙甲乙"), list("X甲乙Y")]
    fragments = candidate_fragments(seqs, [])
    found = _frag_map(fragments)
    assert "甲乙" in found
    assert found["甲乙"] == ((1, 1, 1), (3, 3, 3))
    assert "甲" in found and "乙" in found
    assert "丁" not in found  # 来源 0 中出现两次
    assert "丙" not in found  # 来源 0、2 没有
    assert "X" not in found and "Y" not in found  # 仅一方独有


def test_repeated_fragment_excluded_even_if_others_unique():
    seqs = [list("甲甲X"), list("丙甲"), list("乙甲")]
    fragments = candidate_fragments(seqs, [])
    # 来源 0 中 "甲" 出现两次,不能作为建议;三方没有其它公共非空片段
    assert _frag_map(fragments) == {}


def test_no_common_fragment_returns_empty():
    assert suggest_anchors([list("甲乙"), list("丙丁"), list("戊己")], []) == []
    assert suggest_anchors([[], [], []], []) == []


def test_identical_single_grapheme_repeated_text():
    # 全部为同一字素:只有整个序列作为一个片段恰好出现一次,
    # 任何更短的真子串均重复出现
    fragments = candidate_fragments([["甲"] * 5] * 3, [])
    assert ["".join(f.text) for f in fragments] == ["甲甲甲甲甲"]


def test_candidates_sorted_by_length_desc_then_starts_lexicographic():
    seqs = [list("p甲q"), list("p甲q"), list("p甲q")]
    fragments = candidate_fragments(seqs, [])
    keys = [(-f.length, f.starts[0], f.starts[1], f.starts[2]) for f in fragments]
    assert keys == sorted(keys)
    # 最长片段整体排在最前
    assert fragments[0].text == ("p", "甲", "q")
    # 长度相同的单字素候选按起点字典序:p(0) < 甲(1) < q(2)
    singles = [f for f in fragments if f.length == 1]
    assert ["".join(f.text) for f in singles] == ["p", "甲", "q"]


def test_length_tie_breaks_by_three_way_start_tuple():
    # 两个长度 2 的片段 "甲乙" 与 "丙丁",三方起点相同排列;按起点元组字典序
    seqs = [list("甲乙丙丁"), list("丙丁甲乙"), list("甲乙丙丁")]
    fragments = candidate_fragments(seqs, [])
    longs = [f for f in fragments if f.length == 2]
    assert ["".join(f.text) for f in longs] == ["甲乙", "丙丁"]
    # "甲乙" 起点 (0,2,0),"丙丁" 起点 (2,0,2):先比来源 0,0 < 2
    assert longs[0].starts == (0, 2, 0)
    assert longs[1].starts == (2, 0, 2)


# ---------------------------------------------------------------- 链选择

def test_chain_prefers_long_fragment_over_many_short():
    # 完整三方相同的长片段胜出,即使它只含一个候选
    seqs = [list("甲乙丙丁"), list("甲乙丙丁"), list("甲乙丙丁")]
    suggestions = suggest_anchors(seqs, [])
    assert len(suggestions) == 1
    s = suggestions[0]
    assert s.text == "甲乙丙丁"
    assert s.positions == [[0, 4], [0, 4], [0, 4]]
    assert s.anchor == {"positions": [0, 0, 0]}


def test_chain_maximizes_total_coverage():
    # 两条互不重叠的共享唯一片段,总覆盖 2+2=4,且严格单调
    seqs = [list("甲乙X丙丁"), list("X甲乙丙丁"), list("甲乙丙丁X")]
    suggestions = suggest_anchors(seqs, [])
    assert [s.text for s in suggestions] == ["甲乙", "丙丁"]
    assert suggestions[0].positions == [[0, 2], [1, 3], [0, 2]]
    assert suggestions[1].positions == [[3, 5], [3, 5], [2, 4]]
    # 首字素锚点可直接提交
    assert [s.anchor for s in suggestions] == [
        {"positions": [0, 1, 0]},
        {"positions": [3, 3, 2]},
    ]


def test_chain_skips_fragment_that_violates_monotonicity_in_one_source():
    # "共同" 在三方位置在来源 1 中与其它片段顺序相反,不能同链
    seqs = [list("甲共同乙"), list("乙共同甲"), list("甲共同乙")]
    suggestions = suggest_anchors(seqs, [])
    # "共同"(长度 2)覆盖最大;无法与任一单字素组合(甲乙在来源 1 顺序相反)
    assert [s.text for s in suggestions] == ["共同"]
    assert suggestions[0].anchor == {"positions": [1, 1, 1]}


def test_chain_tie_prefers_fewer_candidates():
    # "长段"(2 簇)与两个不相邻单簇总覆盖相同;候选数更少者胜
    seqs = [list("长段X"), list("X长段"), list("长段X")]
    # 共享唯一字符:长、段(组成"长段")、X;另有"长段"整体
    # "长段"覆盖 2;单簇链中 长 与 X 在来源 1 顺序为 X<长,不可同链
    suggestions = suggest_anchors(seqs, [])
    assert [s.text for s in suggestions] == ["长段"]


def test_chain_tie_prefers_smaller_rank_sequence():
    # 全部为三方各出现一次的单字素,最优覆盖为 2;同分单簇链多条,
    # 取候选排名序列(稳定排序下标)字典序最小者
    seqs = [list("prqs"), list("rpsq"), list("prsq")]
    # 稳定排序后排名: p(0,1,0)=0, r(1,0,1)=1, q(2,3,3)=2, s(3,2,2)=3
    # 可行链(三方严格递增):{p,q} 序列 (0,2) 与 {p,s} 序列 (0,3) -> 选 (0,2)
    suggestions = suggest_anchors(seqs, [])
    assert [s.text for s in suggestions] == ["p", "q"]
    assert suggestions[0].anchor == {"positions": [0, 1, 0]}
    assert suggestions[1].anchor == {"positions": [2, 3, 3]}


def test_chain_suggestions_in_physical_order():
    seqs = [list("甲乙X丙丁Y"), list("X甲乙丙丁Y"), list("甲乙丙丁XY")]
    suggestions = suggest_anchors(seqs, [])
    # 建议链在每个来源上都严格单调:相邻片段区间不重叠且先后一致
    for a, b in zip(suggestions, suggestions[1:]):
        for s in range(3):
            assert a.positions[s][1] <= b.positions[s][0]
    # 返回顺序即来源 0 上的物理先后
    starts0 = [s.positions[0][0] for s in suggestions]
    assert starts0 == sorted(starts0)


# ---------------------------------------------------------------- 既有锚点

def test_existing_anchor_excludes_overlapping_fragment():
    # 已保存锚点 (1,1,1) 把序列切为左右两个间隙;建议不得覆盖锚点簇
    seqs = [list("x甲乙yz"), list("x甲乙yz"), list("x甲乙yz")]
    suggestions = suggest_anchors(seqs, [(1, 1, 1)])
    assert suggestions, "锚点两侧仍有可建议的唯一片段"
    for s in suggestions:
        for k in range(3):
            start, end = s.positions[k]
            # 不覆盖锚点簇 1
            assert not (start <= 1 < end)
            # 不与锚点共址(起点严格大于 1,或片段整体在锚点之前)
            assert end <= 1 or start > 1
    assert all(s.text != "甲乙" for s in suggestions)
    # 左间隙只有簇 0,故若左侧入链只能是 "x";右间隙片段均在锚点之后
    for s in suggestions:
        if s.positions[0][1] <= 1:
            assert s.text == "x"
        else:
            assert s.positions[0][0] > 1


def test_suggestions_merged_with_anchors_stay_strictly_increasing():
    rng = random.Random(99)
    for _ in range(200):
        alpha = rng.choice(["甲乙", "甲乙丙", "甲乙丙丁戊"])
        seqs = [[rng.choice(alpha) for _ in range(rng.randint(0, 8))] for _ in range(3)]
        anchors = []
        prev = [-1, -1, -1]
        for _ in range(rng.randint(0, 2)):
            pos = []
            ok = True
            for s in range(3):
                lo, hi = prev[s] + 1, len(seqs[s]) - 1
                if lo > hi:
                    ok = False
                    break
                pos.append(rng.randint(lo, hi))
            if not ok:
                break
            anchors.append(tuple(pos))
            prev = pos
        suggestions = suggest_anchors(seqs, anchors)
        suggested = [tuple(s.anchor["positions"]) for s in suggestions]
        merged = sorted(anchors + suggested, key=lambda p: p[0])
        # 合并后按来源 0 排序的列表,在每个来源上都严格递增(同间隙 + 严格
        # 边界保证不共址、不回退)
        for s in range(3):
            coords = [p[s] for p in merged]
            assert coords == sorted(coords)
            assert len(set(coords)) == len(coords)
            if seqs[s]:
                assert all(0 <= c < len(seqs[s]) for c in coords)
        # 进一步:每条建议位于某一对相邻锚点之间的开区间内,三方为同一间隙
        for sug in suggestions:
            starts = tuple(sug.anchor["positions"])
            for s in range(3):
                earlier = [a[s] for a in anchors if a[s] < starts[s]]
                later = [a[s] for a in anchors if a[s] > starts[s]]
                assert max(earlier, default=-1) < starts[s]
                assert sug.positions[s][1] <= (min(later) if later else len(seqs[s]))


def test_fragment_crossing_anchor_gap_is_excluded():
    # 片段在来源 0/2 位于锚点之后、在来源 1 位于锚点之前:跨越锚点间隙,
    # 合并后会破坏来源 0/1 的递增性,必须排除
    seqs = [list("锚共同"), list("共同锚"), list("锚共同")]
    suggestions = suggest_anchors(seqs, [(0, 2, 0)])
    assert all(s.text != "共同" for s in suggestions)


def test_fragment_touching_anchor_boundaries():
    # 片段 [1,2) 紧贴锚点 0 之后(起点=0+1)合法;片段终点恰为右锚点位置则
    # 与右锚点共址,合并后不严格递增,必须排除
    seqs = [list("锚甲乙丙"), list("锚甲乙丙"), list("锚甲乙丙")]
    suggestions = suggest_anchors(seqs, [(0, 0, 0), (3, 3, 3)])
    by_text = {s.text: s for s in suggestions}
    assert "甲" in by_text and by_text["甲"].anchor == {"positions": [1, 1, 1]}
    # [1,3) 终点=3 与锚点 (3,3,3) 共址,排除;覆盖锚点簇的片段同样排除
    assert "甲乙" not in by_text
    assert "锚甲乙" not in by_text
    assert "甲乙丙" not in by_text
    for s in suggestions:
        for k in range(3):
            assert not (s.positions[k][0] <= 0 < s.positions[k][1])
            assert not (s.positions[k][0] <= 3 < s.positions[k][1])


# ---------------------------------------------------------------- 字素簇

def test_grapheme_clusters_counted_as_units():
    family = "👨‍👩‍👧‍👦"
    seqs = [[family, "甲", family], [family, "甲"], ["甲", family]]
    # family 在来源 0 出现两次 -> 不作建议;"甲" 三方各一次 -> 可建议
    suggestions = suggest_anchors(seqs, [])
    assert [s.text for s in suggestions] == ["甲"]
    assert suggestions[0].positions == [[1, 2], [1, 2], [0, 1]]


# ---------------------------------------------------------------- 对拍参考

def _brute_chain(fragments):
    """O(M^2) 三维加权区间调度参考实现,与树状数组实现对拍。"""
    order = sorted(range(len(fragments)), key=lambda r: fragments[r].starts)
    best: dict[int, tuple] = {}

    def score(st):
        return (-st[0], st[1], st[2])

    for r in order:
        f = fragments[r]
        pred = (0, 0, ())
        for q in order:
            if q in best and all(
                fragments[q].ends[s] <= f.starts[s] for s in range(3)
            ) and score(best[q]) < score(pred):
                pred = best[q]
        best[r] = (pred[0] + f.length, pred[1] + 1, tuple(sorted(pred[2] + (r,))))
    overall = (0, 0, ())
    for st in best.values():
        if score(st) < score(overall):
            overall = st
    return list(overall[2])


def test_fuzz_chain_selection_matches_brute_force():
    rng = random.Random(20260920)
    for _ in range(400):
        alpha = rng.choice(["甲乙", "甲乙丙", "甲乙丙丁戊"])
        seqs = [[rng.choice(alpha) for _ in range(rng.randint(0, 9))] for _ in range(3)]
        anchors = []
        prev = [-1, -1, -1]
        for _ in range(rng.randint(0, 2)):
            pos = []
            ok = True
            for s in range(3):
                lo, hi = prev[s] + 1, len(seqs[s]) - 1
                if lo > hi:
                    ok = False
                    break
                pos.append(rng.randint(lo, hi))
            if not ok:
                break
            anchors.append(tuple(pos))
            prev = pos
        fragments = candidate_fragments(seqs, anchors)
        # 候选内容与唯一性不变量
        for f in fragments:
            for s in range(3):
                assert tuple(seqs[s][f.starts[s]:f.ends[s]]) == f.text
        keys = [(-f.length, *f.starts) for f in fragments]
        assert keys == sorted(keys)
        assert select_chain(fragments) == _brute_chain(fragments)


# ---------------------------------------------------------------- API

@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.store import CaseStore

    store = CaseStore(str(tmp_path / "cases.db"))
    monkeypatch.setattr("app.main.store", store)
    return TestClient(app)


def _create(client, texts, anchors=None):
    payload = {"texts": texts}
    if anchors is not None:
        payload["anchors"] = anchors
    resp = client.post("/cases", json=payload)
    assert resp.status_code == 201
    return resp.json()


def test_api_suggestions_happy_path(client):
    case = _create(client, ["甲乙X丙丁", "X甲乙丙丁", "甲乙丙丁X"])
    resp = client.get(f"/cases/{case['case_id']}/anchor-suggestions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["case_id"] == case["case_id"]
    assert body["revision"] == 0
    assert set(body) == {"case_id", "revision", "suggestions"}
    suggestions = body["suggestions"]
    assert [s["text"] for s in suggestions] == ["甲乙", "丙丁"]
    s0 = suggestions[0]
    assert set(s0) == {"text", "positions", "anchor"}
    assert s0["positions"] == [[0, 2], [1, 3], [0, 2]]
    assert s0["anchor"] == {"positions": [0, 1, 0]}
    assert suggestions[1]["anchor"] == {"positions": [3, 3, 2]}


def test_api_suggestions_are_submittable_anchors(client):
    texts = ["甲乙X丙丁", "X甲乙丙丁", "甲乙丙丁X"]
    case = _create(client, texts)
    body = client.get(f"/cases/{case['case_id']}/anchor-suggestions").json()
    anchors = [s["anchor"] for s in body["suggestions"]]
    # 建议锚点可直接提交给 anchored 接口且被标记为锚点列
    resp = client.post(
        "/reconcile/anchored", json={"texts": texts, "anchors": anchors}
    )
    assert resp.status_code == 200
    anchored_cols = [c for c in resp.json()["columns"] if c["anchored"]]
    assert len(anchored_cols) == 2
    assert [[s["position"] for s in c["sources"]] for c in anchored_cols] == [
        [0, 1, 0],
        [3, 3, 2],
    ]


def test_api_suggestions_exclude_saved_anchors_and_stay_monotonic(client):
    texts = ["x甲乙y", "x甲乙y", "x甲乙y"]
    case = _create(client, texts, anchors=[{"positions": [1, 1, 1]}])
    body = client.get(f"/cases/{case['case_id']}/anchor-suggestions").json()
    assert all(s["text"] != "甲乙" for s in body["suggestions"])
    # 合并既有锚点后,各来源严格递增
    merged = [(1, 1, 1)] + [
        tuple(s["anchor"]["positions"]) for s in body["suggestions"]
    ]
    merged.sort(key=lambda p: p[0])
    for s in range(3):
        coords = [p[s] for p in merged]
        assert coords == sorted(coords)
        assert len(set(coords)) == len(coords)


def test_api_suggestions_empty_when_no_common_fragment(client):
    case = _create(client, ["甲甲", "乙乙", "丙丙"])
    resp = client.get(f"/cases/{case['case_id']}/anchor-suggestions")
    assert resp.status_code == 200
    assert resp.json()["suggestions"] == []
    assert resp.json()["revision"] == 0


def test_api_suggestions_missing_case_404(client):
    assert client.get("/cases/no-such-case/anchor-suggestions").status_code == 404


def test_api_suggestions_are_read_only(client):
    texts = ["甲乙", "甲乙", "甲乙"]
    case = _create(client, texts)
    before = client.get(f"/cases/{case['case_id']}").json()
    for _ in range(2):
        resp = client.get(f"/cases/{case['case_id']}/anchor-suggestions")
        assert resp.status_code == 200
    after = client.get(f"/cases/{case['case_id']}").json()
    assert after == before  # revision、anchors、结果均不变


def test_api_suggestions_reflect_saved_anchor_change(client):
    # 带锚点新建的案件,建议随库存锚点变化(只读案件本身,但锚点来自案件状态)
    # 建议随案件已保存锚点变化:无锚点时完整唯一片段 "x甲乙y" 可建议,
    # 锚点 (2,2,2) 落在其中后任何包含该簇的片段都被排除
    case_plain = _create(client, ["x甲乙y"] * 3)
    plain = client.get(
        f"/cases/{case_plain['case_id']}/anchor-suggestions"
    ).json()
    assert any(s["text"] == "x甲乙y" for s in plain["suggestions"])

    case_anchored = _create(
        client, ["x甲乙y"] * 3, anchors=[{"positions": [2, 2, 2]}]
    )
    body = client.get(
        f"/cases/{case_anchored['case_id']}/anchor-suggestions"
    ).json()
    # 片段不得覆盖锚点所在簇;仅锚点两侧的单簇/前缀片段可建议
    assert all(
        not (s["positions"][k][0] <= 2 < s["positions"][k][1])
        for s in body["suggestions"]
        for k in range(3)
    )
    assert body["revision"] == 0
