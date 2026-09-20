"""案件锚点建议的测试:候选规则、链选择、既有锚点兼容、只读性与 API。"""
from __future__ import annotations

import itertools
import random
import unicodedata

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store import CaseStore
from app.suggest import suggest_anchors

TEXTS = ["甲乙丙丁", "X甲乙丙Y", "甲乙丙Z"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    """每个用例独立的 SQLite 库,并替换应用全局 store。"""
    store = CaseStore(str(tmp_path / "cases.db"))
    monkeypatch.setattr("app.main.store", store)
    return TestClient(app)


def _create(client, texts=TEXTS, anchors=None):
    payload = {"texts": texts}
    if anchors is not None:
        payload["anchors"] = [{"positions": list(p)} for p in anchors]
    resp = client.post("/cases", json=payload)
    assert resp.status_code == 201
    return resp.json()


def _suggest(client, case_id):
    resp = client.get(f"/cases/{case_id}/anchor-suggestions")
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------- 朴素参考实现

def _naive_suggest(seqs, anchors):
    """暴力参考实现:枚举全部子串验证候选,枚举全部子集验证最优链。"""
    a, b, c = seqs

    def occurrences(seq, sub):
        n = len(sub)
        return [p for p in range(len(seq) - n + 1) if tuple(seq[p : p + n]) == sub]

    # 候选:三方一致、各文本唯一、双向极大的连续公共片段
    candidates = {}
    for i in range(len(a)):
        for j in range(len(b)):
            for k in range(len(c)):
                if not (a[i] == b[j] == c[k]):
                    continue
                length = 0
                while (
                    i + length < len(a)
                    and j + length < len(b)
                    and k + length < len(c)
                    and a[i + length] == b[j + length] == c[k + length]
                ):
                    length += 1
                if i and j and k and a[i - 1] == b[j - 1] == c[k - 1]:
                    continue
                content = tuple(a[i : i + length])
                if content in candidates:
                    continue
                if (
                    len(occurrences(a, content))
                    == len(occurrences(b, content))
                    == len(occurrences(c, content))
                    == 1
                ):
                    candidates[content] = (i, j, k, length)
    cands = [(content, *pos) for content, pos in candidates.items()]

    # 既有锚点过滤:不覆盖锚点位置,且与每个锚点三方次序一致
    def eligible(seg):
        _, i, j, k, length = seg
        for anchor in anchors:
            before = after = False
            for s in range(3):
                start, end = seg[1 + s], seg[1 + s] + length
                if start <= anchor[s] < end:
                    return False
                if anchor[s] < start:
                    before = True
                else:
                    after = True
            if before and after:
                return False
        return True

    cands = [seg for seg in cands if eligible(seg)]
    # 候选排序:长度降序、三方起点字典序;名次即排序后下标
    cands.sort(key=lambda seg: (-seg[4], seg[1], seg[2], seg[3]))

    # 暴力枚举子集,选 (-总覆盖, 候选数, 名次序列) 最小的互不重叠单调链
    best_key, best_chain = None, ()
    for r in range(len(cands) + 1):
        for subset in itertools.combinations(range(len(cands)), r):
            chosen = sorted(
                (cands[t] for t in subset), key=lambda seg: (seg[1], seg[2], seg[3])
            )
            if any(
                chosen[t][1 + s] + chosen[t][4] > chosen[t + 1][1 + s]
                for t in range(len(chosen) - 1)
                for s in range(3)
            ):
                continue
            key = (
                -sum(seg[4] for seg in chosen),
                len(chosen),
                tuple(sorted(subset)),
            )
            if best_key is None or key < best_key:
                best_key, best_chain = key, chosen
    return best_chain


def _expected_response(seqs, anchors):
    chain = _naive_suggest(seqs, anchors)
    return {
        "covered_graphemes": sum(seg[4] for seg in chain),
        "suggestions": [
            {
                "content": "".join(seqs[0][seg[1] : seg[1] + seg[4]]),
                "starts": [seg[1], seg[2], seg[3]],
                "ends": [seg[1] + seg[4], seg[2] + seg[4], seg[3] + seg[4]],
                "length": seg[4],
                "anchor": {"positions": [seg[1], seg[2], seg[3]]},
            }
            for seg in chain
        ],
    }


# ---------------------------------------------------------------- 核心规则

def test_basic_unique_common_segment():
    result = suggest_anchors([list(t) for t in TEXTS], [])
    assert result["covered_graphemes"] == 3
    assert result["suggestions"] == [
        {
            "content": "甲乙丙",
            "starts": [0, 1, 0],
            "ends": [3, 4, 3],
            "length": 3,
            "anchor": {"positions": [0, 1, 0]},
        }
    ]


def test_segment_appearing_twice_is_rejected():
    # "甲乙" 在三份文本中各出现两次,不满足唯一性
    result = suggest_anchors([list(t) for t in ["甲乙甲乙", "甲乙丙甲乙", "甲乙X甲乙"]], [])
    assert result == {"covered_graphemes": 0, "suggestions": []}


def test_only_maximal_segments_are_candidates():
    # 三份文本完全相同:唯一候选是整段,子片段不单独成为候选
    result = suggest_anchors([list("甲乙丙")] * 3, [])
    assert [s["content"] for s in result["suggestions"]] == ["甲乙丙"]
    assert result["suggestions"][0]["length"] == 3


def test_chain_skips_order_inversions():
    # "甲"(0,2,0) 与 "乙丙"(1,0,1) 在来源 0/1 中次序倒置,不能同链
    result = suggest_anchors([list(t) for t in ["甲乙丙丁", "乙丙甲丁", "甲乙丙丁"]], [])
    assert [s["content"] for s in result["suggestions"]] == ["乙丙", "丁"]
    assert result["covered_graphemes"] == 3


def test_tie_prefers_fewer_candidates():
    # {甲乙}(覆盖 2)与 {丙, 丁}(覆盖 2)同分,候选数更少者胜
    result = suggest_anchors([list(t) for t in ["甲乙丙子丁", "丙丑丁甲乙", "甲乙丙寅丁"]], [])
    assert [s["content"] for s in result["suggestions"]] == ["甲乙"]


def test_tie_prefers_smaller_rank_sequence():
    # {甲乙} 与 {丙丁} 覆盖、候选数均同;按候选排序(长度降序、起点字典序)
    # 甲乙名次更靠前,其名次序列更小
    result = suggest_anchors([list(t) for t in ["甲乙丙丁X", "丙丁甲乙Y", "甲乙丙丁Z"]], [])
    assert [s["content"] for s in result["suggestions"]] == ["甲乙"]


def test_empty_texts_yield_no_suggestions():
    assert suggest_anchors([[], [], []], []) == {"covered_graphemes": 0, "suggestions": []}


# ---------------------------------------------------------------- 既有锚点

def test_segment_covering_saved_anchor_is_excluded():
    # 锚点 (1,2,1) 落在 "甲乙丙" 片段三方区间内,该片段被排除
    result = suggest_anchors([list(t) for t in TEXTS], [(1, 2, 1)])
    assert result == {"covered_graphemes": 0, "suggestions": []}


def test_segment_inconsistent_with_saved_anchor_is_excluded():
    # 锚点 (0,2,0):对 "丙丁"(2,0,2) 而言来源 0 在其前、来源 1 在其后,次序不一致
    result = suggest_anchors([list(t) for t in ["甲乙丙丁", "丙丁甲乙", "甲乙丙丁"]], [(0, 2, 0)])
    assert result == {"covered_graphemes": 0, "suggestions": []}


def test_anchor_in_gap_keeps_compatible_chain():
    # 锚点 (3,3,3) 排除 "丁" 片段,但 "乙丙" 与其次序一致,仍被建议
    result = suggest_anchors([list(t) for t in ["甲乙丙丁", "乙丙甲丁", "甲乙丙丁"]], [(3, 3, 3)])
    assert [s["content"] for s in result["suggestions"]] == ["乙丙"]
    assert result["suggestions"][0]["anchor"] == {"positions": [1, 0, 1]}


def test_suggestions_merge_with_saved_anchors_stays_strictly_increasing():
    rng = random.Random(7)
    for _ in range(60):
        alpha = rng.choice(["甲乙", "甲乙丙"])
        seqs = [[rng.choice(alpha) for _ in range(rng.randint(0, 6))] for _ in range(3)]
        anchors = []
        prev = [-1, -1, -1]
        for _ in range(rng.randint(0, 2)):
            pos = []
            for s in range(3):
                lo, hi = prev[s] + 1, len(seqs[s]) - 1
                if lo > hi:
                    break
                pos.append(rng.randint(lo, hi))
            if len(pos) < 3:
                break
            anchors.append(tuple(pos))
            prev = pos
        result = suggest_anchors(seqs, anchors)
        merged = sorted(
            [tuple(s["anchor"]["positions"]) for s in result["suggestions"]] + anchors
        )
        for s in range(3):
            positions = [p[s] for p in merged]
            assert all(x < y for x, y in zip(positions, positions[1:]))


# ---------------------------------------------------------------- 随机对拍

def test_fuzz_against_naive_reference():
    rng = random.Random(2024)
    for _ in range(150):
        alpha = rng.choice(["甲乙", "甲乙丙"])
        seqs = [[rng.choice(alpha) for _ in range(rng.randint(0, 6))] for _ in range(3)]
        anchors = []
        prev = [-1, -1, -1]
        for _ in range(rng.randint(0, 2)):
            pos = []
            for s in range(3):
                lo, hi = prev[s] + 1, len(seqs[s]) - 1
                if lo > hi:
                    break
                pos.append(rng.randint(lo, hi))
            if len(pos) < 3:
                break
            anchors.append(tuple(pos))
            prev = pos
        assert suggest_anchors(seqs, anchors) == _expected_response(seqs, anchors), (
            seqs,
            anchors,
        )


# ---------------------------------------------------------------- API

def test_api_suggestions_happy_path(client):
    case = _create(client)
    body = _suggest(client, case["case_id"])
    assert body["case_id"] == case["case_id"]
    assert body["revision"] == 0
    assert body["covered_graphemes"] == 3
    assert body["suggestions"] == [
        {
            "content": "甲乙丙",
            "starts": [0, 1, 0],
            "ends": [3, 4, 3],
            "length": 3,
            "anchor": {"positions": [0, 1, 0]},
        }
    ]
    assert set(body) == {"case_id", "revision", "covered_graphemes", "suggestions"}


def test_api_suggestions_are_read_only(client):
    case = _create(client)
    before = client.get(f"/cases/{case['case_id']}").json()
    first = _suggest(client, case["case_id"])
    second = _suggest(client, case["case_id"])
    assert first == second
    after = client.get(f"/cases/{case['case_id']}").json()
    # 不修改案件:revision、锚点、裁决与当前结果均不变
    assert after == before
    assert after["revision"] == 0
    assert after["anchors"] == []


def test_api_suggestions_respect_saved_anchors(client):
    case = _create(client, anchors=[(1, 2, 1)])
    body = _suggest(client, case["case_id"])
    assert body["suggestions"] == []
    assert body["covered_graphemes"] == 0


def test_api_suggestions_after_adjudication_uses_current_revision(client):
    case = _create(client, texts=["甲丙", "甲丁", "甲乙"])
    resp = client.post(
        f"/cases/{case['case_id']}/adjudications",
        json={"expected_revision": 0, "decisions": [{"signature": [1, 1, 1], "source": 0}]},
    )
    assert resp.status_code == 200
    body = _suggest(client, case["case_id"])
    assert body["revision"] == 1
    assert [s["content"] for s in body["suggestions"]] == ["甲"]
    assert client.get(f"/cases/{case['case_id']}").json()["revision"] == 1


def test_api_suggestions_missing_case_404(client):
    assert client.get("/cases/does-not-exist/anchor-suggestions").status_code == 404


def test_api_suggestions_nfc_normalized(client):
    # 库存文本含 NFD 形式;建议内容按 NFC 规范化后的字素簇给出
    nfd = unicodedata.normalize("NFD", "é甲")
    nfc = unicodedata.normalize("NFC", "é甲")
    case = _create(client, texts=[nfd, nfc, nfc])
    body = _suggest(client, case["case_id"])
    assert [s["content"] for s in body["suggestions"]] == [nfc]
    assert body["suggestions"][0]["length"] == 2


def test_api_suggestions_grapheme_clusters(client):
    # 家庭 emoji 是单个字素簇:片段长度按簇计,位置按簇下标计
    family = "👨‍👩‍👧‍👦"
    case = _create(client, texts=[family + "甲乙", "丙" + family + "甲乙", family + "甲乙"])
    body = _suggest(client, case["case_id"])
    assert body["suggestions"] == [
        {
            "content": family + "甲乙",
            "starts": [0, 1, 0],
            "ends": [3, 4, 3],
            "length": 3,
            "anchor": {"positions": [0, 1, 0]},
        }
    ]


def test_api_suggestions_anchor_directly_submittable(client):
    # 建议的首字素锚点与既有锚点合并后,可直接通过锚点校验
    case = _create(client, texts=["甲乙丙丁", "乙丙甲丁", "甲乙丙丁"], anchors=[(3, 3, 3)])
    body = _suggest(client, case["case_id"])
    anchors = [s["anchor"] for s in body["suggestions"]] + case["anchors"]
    anchors.sort(key=lambda a: a["positions"][0])
    resp = client.post(
        "/reconcile/anchored",
        json={"texts": ["甲乙丙丁", "乙丙甲丁", "甲乙丙丁"], "anchors": anchors},
    )
    assert resp.status_code == 200
