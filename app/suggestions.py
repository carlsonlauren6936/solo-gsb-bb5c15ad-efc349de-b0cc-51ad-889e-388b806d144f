"""案件锚点建议:在三份字素簇序列中寻找可直接采纳的互不重叠公共片段。

候选为三方内容完全相同、且在各自序列中均恰好出现一次的非空连续公共
片段;已保存锚点覆盖、共址或跨越其切出区间的片段不予考虑
(见 :func:`candidate_fragments`)。

候选按长度降序、其次三方起点字典序稳定排序并取得排名。再从候选中选择
一条三方互不重叠、且在每个来源上都严格递增的"单调链",使链上片段覆盖
的总字素数最大;同分时依次偏好:

  1. 候选数更少的链;
  2. 排序序列(链上候选在上述稳定排序中的排名,按升序)字典序更小的链。

选择结果与既有锚点合并后,各来源位置仍严格递增,可直接作为锚点提交给
``/cases`` 等接口;本模块只读,不修改任何案件数据。

序列长度上限为 120,最坏候选数约为 O(n^3),朴素 O(M^2) 的区间动态规划
无法承受;这里采用按第一维终点扫描 + 二维树状数组(其余两维前缀最大值)
的三维加权区间调度,总复杂度约 O(M log^2 n)。
"""
from __future__ import annotations

from bisect import bisect_right
from collections import namedtuple

#: 一个候选片段:内容、长度、三来源起点(含)与终点(不含)
Fragment = namedtuple("Fragment", "text length starts ends")

#: 一个建议:片段内容、三来源 [start, end) 区间与可直接提交的首字素锚点
Suggestion = namedtuple("Suggestion", "text positions anchor")

#: 动态规划状态:链覆盖字素总数、候选数、链上候选的排名序列
State = namedtuple("State", "cover count ranks")

_EMPTY = State(0, 0, ())


def _single_occurrences(seq: list[str]) -> dict[tuple[str, ...], int]:
    """列出在 seq 中恰好出现一次的非空子串:子串 -> 起点。

    出现多次记为 -1。序列至多 120 个簇,用元组键直接枚举 O(n^2) 即可。
    """
    occurrences: dict[tuple[str, ...], int] = {}
    n = len(seq)
    for i in range(n):
        key: tuple[str, ...] = ()
        for j in range(i, n):
            key += (seq[j],)
            # 键统一为不同长度的字素簇元组,按相等性(而非仅哈希)区分:
            # 不同元组即使哈希碰撞,dict 也不会误判为同一子串
            if key in occurrences:
                occurrences[key] = -1
            else:
                occurrences[key] = i
    return {key: pos for key, pos in occurrences.items() if pos >= 0}


def candidate_fragments(
    seqs: list[list[str]], anchors: list[tuple[int, int, int]]
) -> list[Fragment]:
    """枚举全部合格候选并稳定排序。

    片段须在三方序列中均只出现一次(出现多次时无法唯一定位,三方内容即
    便相同也不予建议);并且必须整体落在相邻两个既有锚点切出的同一个
    开间隙内,即对每个来源都满足 ``prev[s] < start[s]`` 且
    ``end[s] < next[s]``(首锚点哨兵为 -1,末锚点哨兵为 len(seq))。
    由此同时排除了与已保存锚点共址或覆盖锚点簇的片段,并保证建议与
    既有锚点合并后各来源位置严格递增。

    返回按 (长度降序, 起点0, 起点1, 起点2) 排序的稳定列表;长度相同、
    起点相同时内容必相同,顺序完全确定。
    """
    occurrences = [_single_occurrences(seq) for seq in seqs]
    # 以来源 0 的唯一子串为基准,检查另外两方是否同样唯一
    base = occurrences[0]

    # 每来源的既有锚点位置(锚点已保证各来源严格递增),外加首尾哨兵
    bounds: list[list[int]] = []
    for s in range(3):
        bounds.append(sorted(a[s] for a in anchors))

    fragments: list[Fragment] = []
    for key, p0 in base.items():
        p1 = occurrences[1].get(key)
        p2 = occurrences[2].get(key)
        if p1 is None or p2 is None:
            continue
        starts = (p0, p1, p2)
        length = len(key)
        ends = (p0 + length, p1 + length, p2 + length)
        if not _in_same_gap(starts, ends, bounds):
            continue
        fragments.append(Fragment(tuple(key), length, starts, ends))

    fragments.sort(key=lambda f: (-f.length, f.starts[0], f.starts[1], f.starts[2]))
    return fragments


def _in_same_gap(
    starts: tuple[int, int, int],
    ends: tuple[int, int, int],
    bounds: list[list[int]],
) -> bool:
    """三来源是否落在同一段锚点间隙(同一对相邻锚点之间)。

    第 g 个间隙为开区间 (bounds[g-1], bounds[g]):片段起点严格大于左锚点
    (故首字素不与左锚点共址)、终点严格小于右锚点(故片段不覆盖右锚点簇,
    末字素位置也小于右锚点)。两侧均严格,才能保证建议首字素与既有锚点
    合并后各来源位置严格递增;右锚点不存在时哨兵为 +∞。
    """
    gap = bisect_right(bounds[0], starts[0] - 1)
    for s in range(3):
        if bisect_right(bounds[s], starts[s] - 1) != gap:
            return False
        if gap < len(bounds[s]) and ends[s] >= bounds[s][gap]:
            return False
    return True


class _Fenwick2D:
    """二维树状数组,网格点上保存"更优"的 :class:`State`,支持前缀最大值。

    坐标使用实际终点值(1 起始换算),网格尺寸 (n2+1)×(n3+1),逻辑坐标
    0 行/列恒为空白状态(终点至少为 1,不会写入 0)。
    """

    def __init__(self, n2: int, n3: int):
        self._w = n2 + 1
        self._h = n3 + 1
        size = self._w * self._h
        self._cover = [0] * size
        self._count = [0] * size
        self._ranks: list[tuple[int, ...]] = [()] * size

    def update(self, x: int, y: int, state: State) -> None:
        """点 (x, y) 更新为与 state 中的更优者。"""
        w, h = self._w, self._h
        i = x
        while i < w:
            j = y
            base = i * h
            while j < h:
                idx = base + j
                if _state_better(state, self._snapshot(idx)):
                    self._cover[idx] = state.cover
                    self._count[idx] = state.count
                    self._ranks[idx] = state.ranks
                j += j & -j
            i += i & -i

    def query(self, x: int, y: int) -> State:
        """逻辑矩形 [1..x] × [1..y](含 x、y)上的最优状态。"""
        h = self._h
        best = _EMPTY
        i = x
        while i > 0:
            j = y
            base = i * h
            while j > 0:
                idx = base + j
                cand = self._snapshot(idx)
                if _state_better(cand, best):
                    best = cand
                j -= j & -j
            i -= i & -i
        return best

    def _snapshot(self, idx: int) -> State:
        return State(self._cover[idx], self._count[idx], self._ranks[idx])


def _state_better(candidate: State, current: State) -> bool:
    """候选状态是否优于当前状态:覆盖更大;同覆盖候选更少;再同则排名序列更小。"""
    if candidate.cover != current.cover:
        return candidate.cover > current.cover
    if candidate.count != current.count:
        return candidate.count < current.count
    return candidate.ranks < current.ranks


def _insert_sorted(ranks: tuple[int, ...], rank: int) -> tuple[int, ...]:
    """把排名 rank 按升序插入链排名序列(链顺序与排名升序一致,见下)。"""
    lo, hi = 0, len(ranks)
    while lo < hi:
        mid = (lo + hi) // 2
        if ranks[mid] < rank:
            lo = mid + 1
        else:
            hi = mid
    return ranks[:lo] + (rank,) + ranks[lo:]


def select_chain(fragments: list[Fragment]) -> list[int]:
    """从排序后的候选中选出最优互不重叠单调链,返回选中候选的下标(链顺序)。

    候选按 (长度降序, 起点字典序) 排列并取得排名;扫描按第一维终点推进:
    终点不超过当前候选起点的候选先写入二维树状数组,再查询二维前缀
    ``[0, start1) × [0, start2)``,即得到第一维终点不超过 start0、且其余
    两维同样严格在前的最优前驱链——区间在任一来源上不得相交,故用起点
    (而非终点)做严格前缀查询。

    同分比较所用的"排序序列"为链上候选排名按升序排列的元组(链在第一维
    上的物理顺序与排名升序一致,故同时也是链顺序的规范化表示),字典序
    更小者优先。
    """
    if not fragments:
        return []

    n2 = max(f.ends[1] for f in fragments)
    n3 = max(f.ends[2] for f in fragments)
    bit = _Fenwick2D(n2, n3)

    # 写入桶:第一维终点 -> 同终点候选(按排名升序收集)
    buckets: dict[int, list[int]] = {}
    for rank, f in enumerate(fragments):
        buckets.setdefault(f.ends[0], []).append(rank)
    end_order = sorted(buckets)

    states: dict[int, State] = {}
    global_best = _EMPTY

    def flush(end0: int) -> None:
        for rank in buckets[end0]:
            f = fragments[rank]
            bit.update(f.ends[1], f.ends[2], states[rank])

    cursor = 0  # 下一个待写入的终点桶
    # 状态计算随扫描(第一维终点)推进,而非随候选排名:查询某个候选时,
    # 第一维终点更小的候选(无论长度/排名)状态都须已算好并可写入
    scan_order = sorted(range(len(fragments)), key=lambda r: (fragments[r].ends[0], r))
    for rank in scan_order:
        f = fragments[rank]
        # 第一维终点不超过本候选起点的前驱方可参与查询
        while cursor < len(end_order) and end_order[cursor] <= f.starts[0]:
            flush(end_order[cursor])
            cursor += 1
        predecessor = bit.query(f.starts[1], f.starts[2])
        state = State(
            cover=predecessor.cover + f.length,
            count=predecessor.count + 1,
            ranks=_insert_sorted(predecessor.ranks, rank),
        )
        states[rank] = state
        if _state_better(state, global_best):
            global_best = state

    # global_best.ranks 已唯一确定链中成员(升序即链在各来源上的物理顺序)
    return list(global_best.ranks)


def suggest_anchors(
    seqs: list[list[str]], anchors: list[tuple[int, int, int]]
) -> list[Suggestion]:
    """对外入口:返回按链顺序排列的锚点建议(只读,不修改 anchors)。"""
    fragments = candidate_fragments(seqs, anchors)
    chosen = select_chain(fragments)
    suggestions: list[Suggestion] = []
    for rank in chosen:
        f = fragments[rank]
        suggestions.append(
            Suggestion(
                text="".join(f.text),
                positions=[[f.starts[s], f.ends[s]] for s in range(3)],
                anchor={"positions": [f.starts[s] for s in range(3)]},
            )
        )
    return suggestions
