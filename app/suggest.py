"""案件锚点建议:寻找三份文本中极大且唯一的公共片段,并选出最优单调链。

候选片段:三份文本(NFC 字素簇序列)中内容完全相同、且在每份文本中
仅出现一次的连续公共片段,且双向不可扩展(极大):向左或向右再延长一个
字素都会破坏三方一致。由于内容在各文本中唯一,每个候选的内容唯一确定
其在三份文本中的起点。

链选择:候选先按长度降序、三方起点字典序稳定排序(候选名次即该序列的
下标)。在互不重叠(每份文本中区间两两不相交)且单调(三份文本中起点
次序一致)的链中,依次取:
  1. 总覆盖字素数最大(各片段长度之和;每份文本被覆盖的字素簇数相同);
  2. 同分时候选数更少;
  3. 再同分时,按候选排序取得的名次序列(升序)字典序更小。
"""
from __future__ import annotations

from bisect import insort

import numpy as np


def _encode(seq: list[str], vocab: dict[str, int]) -> list[int]:
    """把字素簇序列编码为整数列表,便于按值比较与切片哈希。"""
    codes = []
    for g in seq:
        code = vocab.get(g)
        if code is None:
            code = len(vocab)
            vocab[g] = code
        codes.append(code)
    return codes


def _lcp_table(a: list[int], b: list[int], c: list[int]) -> np.ndarray:
    """lcp[i, j, k] = A[i:]、B[j:]、C[k:] 的最长公共前缀长度(反向递推)。"""
    n1, n2, n3 = len(a), len(b), len(c)
    A = np.asarray(a, dtype=np.int64)
    B = np.asarray(b, dtype=np.int64)
    C = np.asarray(c, dtype=np.int64)
    lcp = np.zeros((n1 + 1, n2 + 1, n3 + 1), dtype=np.int32)
    for i in range(n1 - 1, -1, -1):
        ai = A[i]
        for j in range(n2 - 1, -1, -1):
            if B[j] != ai:
                continue  # 整行保持 0:A[i] != B[j] 时三方不可能一致
            eq = C == ai
            lcp[i, j, :n3] = np.where(eq, 1 + lcp[i + 1, j + 1, 1 : n3 + 1], 0)
    return lcp


def _substring_counter(seq: list[int], length: int) -> dict[tuple, int]:
    """统计文本中每个长度为 length 的连续片段的出现次数(按起点计,允许重叠)。"""
    counter: dict[tuple, int] = {}
    for start in range(len(seq) - length + 1):
        key = tuple(seq[start : start + length])
        counter[key] = counter.get(key, 0) + 1
    return counter


def _common_unique_segments(
    a: list[int], b: list[int], c: list[int]
) -> list[tuple[tuple, int, int, int, int]]:
    """所有极大且三方唯一的公共片段 (content, i, j, k, L)。

    右极大由最长公共前缀保证;左极大要求任一来源在边界或前一字素非三方
    一致。唯一性要求片段内容在每份文本中恰好出现一次。
    """
    lcp = _lcp_table(a, b, c)
    pos_b: dict[int, list[int]] = {}
    for j, code in enumerate(b):
        pos_b.setdefault(code, []).append(j)
    pos_c: dict[int, list[int]] = {}
    for k, code in enumerate(c):
        pos_c.setdefault(code, []).append(k)

    # 按 (i, L) 分组:同组三元组的内容相同,内容若在 B/C 中出现多次必然不唯一
    groups: dict[tuple[int, int], list] = {}
    for i, ai in enumerate(a):
        js = pos_b.get(ai)
        ks_all = pos_c.get(ai)
        if not js or not ks_all:
            continue
        for j in js:
            if i and j and a[i - 1] == b[j - 1]:
                # 左极大还要求 C 的前一字素与公共前驱不同(或 k 在边界)
                pred = a[i - 1]
                ks = [k for k in ks_all if k == 0 or c[k - 1] != pred]
            else:
                ks = ks_all
            for k in ks:
                length = int(lcp[i, j, k])
                key = (i, length)
                if key in groups:
                    groups[key][0] += 1
                else:
                    groups[key] = [1, j, k]

    lengths = {length for (i, length), g in groups.items() if g[0] == 1}
    counters = {
        length: [_substring_counter(seq, length) for seq in (a, b, c)]
        for length in lengths
    }
    segments = []
    for (i, length), (count, j, k) in groups.items():
        if count != 1:
            continue
        content = tuple(a[i : i + length])
        ca, cb, cc = counters[length]
        if ca.get(content, 0) == 1 and cb.get(content, 0) == 1 and cc.get(content, 0) == 1:
            segments.append((content, i, j, k, length))
    return segments


def _eligible(
    segment: tuple[tuple, int, int, int, int], anchors: list[tuple[int, int, int]]
) -> bool:
    """片段与既有锚点兼容:不覆盖任一锚点位置,且与每个锚点在三方保持一致的先后次序。

    满足条件的片段,其首字素锚点与既有锚点合并后各来源位置仍严格递增。
    """
    _, i, j, k, length = segment
    starts = (i, j, k)
    ends = (i + length, j + length, k + length)
    for anchor in anchors:
        before = after = False
        for s in range(3):
            if starts[s] <= anchor[s] < ends[s]:
                return False  # 覆盖既有锚点位置,排除
            if anchor[s] < starts[s]:
                before = True
            else:
                after = True
        if before and after:
            return False  # 与该锚点次序不一致,合并后无法严格递增
    return True


def _select_chain(
    candidates: list[tuple[tuple, int, int, int, int]],
) -> tuple[int, ...]:
    """从已排序(长度降序、三方起点字典序)的候选中选出最优单调链。

    返回候选下标元组,按三方起点升序(即链序)。名次 = 候选在排序序列中的
    下标;状态比较键为 (-总覆盖字素数, 候选数, 名次升序元组),越小越优。
    向等长名次元组插入同一名次保持字典序,故按起点升序递推时以各候选
    结尾的最优链可组合出全局最优。
    """
    m = len(candidates)
    if m == 0:
        return ()
    starts = [(seg[1], seg[2], seg[3]) for seg in candidates]
    lens = [seg[4] for seg in candidates]
    # 按三方起点升序处理:前驱的起点必严格更小,保证只从链上前驱扩展
    order = sorted(range(m), key=lambda q: starts[q])

    def key(state: tuple[int, int, tuple, tuple]) -> tuple:
        total, count, ranks, _ = state
        return (-total, count, ranks)

    # dp[q] = (总长, 候选数, 名次升序元组, 链上下标元组),以 q 结尾的最优链
    dp: dict[int, tuple[int, int, tuple, tuple]] = {}
    for qi, q in enumerate(order):
        best = (lens[q], 1, (q,), (q,))
        for p in order[:qi]:
            if not all(starts[p][s] + lens[p] <= starts[q][s] for s in range(3)):
                continue  # 不兼容:某来源中区间重叠或次序倒置
            p_len, p_cnt, p_ranks, p_chain = dp[p]
            ranks = list(p_ranks)
            insort(ranks, q)
            cand = (p_len + lens[q], p_cnt + 1, tuple(ranks), p_chain + (q,))
            if key(cand) < key(best):
                best = cand
        dp[q] = best
    return min(dp.values(), key=key)[3]


def suggest_anchors(
    seqs: list[list[str]], anchors: list[tuple[int, int, int]]
) -> dict:
    """对三份字素簇序列与既有锚点,给出最优锚点建议链。

    seqs 为三个 NFC 字素簇列表;anchors 为已保存锚点 [(i, j, k), ...](调用方
    保证合法)。返回 {"covered_graphemes": int, "suggestions": [...]},建议按
    三方起点升序,每条含片段内容、三方起止位置与首字素锚点。
    """
    vocab: dict[str, int] = {}
    enc = [_encode(seq, vocab) for seq in seqs]
    segments = _common_unique_segments(*enc)
    eligible = [seg for seg in segments if _eligible(seg, anchors)]
    # 候选排序:长度降序、三方起点字典序
    eligible.sort(key=lambda seg: (-seg[4], seg[1], seg[2], seg[3]))
    chain = _select_chain(eligible)

    suggestions = []
    for idx in chain:
        _, i, j, k, length = eligible[idx]
        suggestions.append(
            {
                "content": "".join(seqs[0][i : i + length]),
                "starts": [i, j, k],
                "ends": [i + length, j + length, k + length],
                "length": length,
                "anchor": {"positions": [i, j, k]},
            }
        )
    return {
        "covered_graphemes": sum(s["length"] for s in suggestions),
        "suggestions": suggestions,
    }
