# 抄本核对 API

供档案员核对三份独立抄录的 Web API:对三段文本做 NFC 规范化后,用三维全局动态规划对齐,生成共识文本,并报告对齐列与总成本。

## 启动

只需一条命令(依赖随镜像构建自动安装):

```bash
docker compose up --build
```

- 服务地址: <http://localhost:8000>
- 交互式文档(Swagger UI): <http://localhost:8000/docs>
- 健康检查: <http://localhost:8000/health>

持久化案件的 SQLite 数据库保存在名为 `case_data` 的数据卷中(容器内 `/data/cases.db`),容器重启/重建后案件仍可查询;`docker compose down -v` 会连同数据卷一起删除。

## 接口

### `POST /cases`

创建持久化核对案件。请求结构与 `/reconcile/anchored` 相同(`texts` 加可选 `anchors`,校验规则一致);服务端把三段文本、锚点、裁决列表(初始为空)、当前结果与递增 `revision` 存入 SQLite,返回 `201` 与完整案件状态,`revision` 初始为 `0`。

```bash
curl -X POST http://localhost:8000/cases \
  -H "Content-Type: application/json" \
  -d '{"texts": ["甲丙", "甲丁", "甲乙"]}'
```

```json
{
  "case_id": "9f0b1c2d3e4f40518a6b7c8d9e0f1a2b",
  "revision": 0,
  "texts": ["甲丙", "甲丁", "甲乙"],
  "anchors": [],
  "decisions": [],
  "result": {
    "consensus": "甲�",
    "total_cost": 3,
    "columns": ["…同 /reconcile/adjudicate 的列结构,含 anchored 标记…"],
    "uncertain": [
      {
        "column": 1,
        "sources": [
          {"grapheme": "丙", "position": 1},
          {"grapheme": "丁", "position": 1},
          {"grapheme": "乙", "position": 1}
        ]
      }
    ],
    "audits": []
  }
}
```

### `GET /cases/{case_id}`

查询案件完整状态(文本、锚点、已生效裁决列表、当前结果与 `revision`);案件落盘于 SQLite,**服务重启后仍可查询**。案件不存在返回 404。

```bash
curl http://localhost:8000/cases/9f0b1c2d3e4f40518a6b7c8d9e0f1a2b
```

### `POST /cases/{case_id}/adjudications`

向案件**追加裁决**。必须提交 `expected_revision`(即客户端此前读到的 `revision`)与至少一条 `decisions`(结构同 `/reconcile/adjudicate`)。服务端在**同一事务**中完成:校验版本 → 合并既有与新增裁决 → 重算对齐并对合并后的全部裁决做原子校验 → 写入并把 `revision` 加一:

- **版本不符**:返回 **409**,`detail.current_revision` 给出服务端当前版本,不写入任何修改;
- **合并后任一裁决失效**(目标列不存在/已共识、与既有或本批裁决签名重复、`source` 指向空位):返回 **409**,`detail` 列出每项失效裁决的全部原因(下标按"既有在前、追加在后"的合并顺序),整体回滚,不写入任何修改;
- **全部通过**:`revision` 加一,返回更新后的完整案件状态,`result` 为对全部已生效裁决重算的结果(`audits` 覆盖全部已生效裁决)。

```bash
curl -X POST http://localhost:8000/cases/9f0b1c2d3e4f40518a6b7c8d9e0f1a2b/adjudications \
  -H "Content-Type: application/json" \
  -d '{"expected_revision": 0, "decisions": [{"signature": [1, 1, 1], "source": 0}]}'
```

```json
{
  "case_id": "9f0b1c2d3e4f40518a6b7c8d9e0f1a2b",
  "revision": 1,
  "texts": ["甲丙", "甲丁", "甲乙"],
  "anchors": [],
  "decisions": [{"signature": [1, 1, 1], "source": 0, "replacement": null}],
  "result": {
    "consensus": "甲丙",
    "total_cost": 3,
    "columns": ["…"],
    "uncertain": [],
    "audits": [
      {"signature": [1, 1, 1], "original": ["丙", "丁", "乙"], "adopted": "丙", "method": "source:0"}
    ]
  }
}
```

版本不符时的 409 响应:

```json
{
  "detail": {
    "error": "revision_conflict",
    "message": "expected_revision=0 与当前版本不符(当前 revision=1),未写入任何修改",
    "current_revision": 1
  }
}
```

### `GET /cases/{case_id}/anchor-suggestions`

**只读**的案件锚点建议:不修改案件(`revision`、锚点、裁决与结果均不变),可反复调用。

- **地址**:`GET http://localhost:8000/cases/{case_id}/anchor-suggestions`
- **参数**:仅路径参数 `case_id`(创建案件时返回的 ID),无请求体;案件不存在返回 404。
- **响应**:`case_id`、`revision`(生成建议时读取的版本)、`covered_graphemes`(建议链在三份文本中**各自**覆盖的字素簇总数,各来源相同)与 `suggestions`(按三方起点升序)。每条建议含:
  - `content`:片段内容(三份文本完全一致,NFC);
  - `starts` / `ends`:片段在 `texts[0/1/2]` 中的起止字素簇下标(0 起始,起点含、终点不含);
  - `length`:片段长度(字素簇数);
  - `anchor`:可直接提交的首字素锚点 `{"positions": [i, j, k]}`,与既有锚点合并后各来源位置仍严格递增,可直接放入 `anchors` 用于 `/reconcile/anchored` 或新案件。

```bash
curl http://localhost:8000/cases/9f0b1c2d3e4f40518a6b7c8d9e0f1a2b/anchor-suggestions
```

```json
{
  "case_id": "9f0b1c2d3e4f40518a6b7c8d9e0f1a2b",
  "revision": 0,
  "covered_graphemes": 1,
  "suggestions": [
    {
      "content": "甲",
      "starts": [0, 0, 0],
      "ends": [1, 1, 1],
      "length": 1,
      "anchor": {"positions": [0, 0, 0]}
    }
  ]
}
```

建议规则:对库存三份文本(NFC 字素簇)寻找**三方内容完全相同、且在各自文本中仅出现一次**的连续公共片段,候选取**双向不可再延长**的极大片段;按**长度降序、三方起点字典序**稳定排序后,选出**总覆盖字素数最大的互不重叠单调链**(每份文本中区间两两不相交且起点次序一致);同分时依次取**候选数更少**、**名次序列(按候选排序)字典序更小**的方案。已保存锚点覆盖的片段被排除,且每条建议与每个既有锚点在三方保持一致的先后次序,保证合并后各来源位置严格递增。

### `POST /reconcile/adjudicate`

人工裁决接口。请求在三段文本之外可携带 `anchors`(同 `/reconcile/anchored`)与 `decisions`(裁决列表,可省略)。服务端先按本次请求**重算对齐**,再一次性校验全部裁决,全部有效才生效:

- **坐标签名定位**:每项裁决以 `signature` 给出该列在三份文本中的**原始字素簇位置**(0 起始,与 `/reconcile` 响应中 `uncertain[].sources[].position` 一致;该列未消费的来源记 `null`)。按原始坐标而非列序号定位,锚点导致列序/列数变化时裁决不会错落到其他位置。
- **二选一给出采用值**:`source` 为 `0/1/2` 时采用该列该来源的**非空字素**;`replacement` 为替换字符串时,服务端做 NFC 规范化,要求规范化后**恰好为一个字素簇**(如 `👨‍👩‍👧‍👦` 合法,`甲乙` 非法)。
- **一次性原子校验**:目标列必须存在、目标列仍为 `uncertain`、同一签名不得重复裁决、`source` 不得指向空位;任一项失效返回 **409**(响应 `detail` 列出每项失效裁决的全部原因),**不产出部分结果**。请求结构问题(签名长度/负值、`source` 越界、同时/均未给 `source` 与 `replacement`、替换值非单字素簇、锚点非法、超出 120 簇等)返回 **422**。
- 成功响应在 `/reconcile/anchored` 结构上增加 `audits`;`consensus` 为裁决后的完整文本,`uncertain` 仅保留**尚未裁决**的列,列内 `consensus` 同步更新;`audits` 按请求顺序记录每项裁决的 `signature`(坐标签名)、`original`(裁决前三来源原字素,空位 `null`)、`adopted`(采用值)与 `method`(`source:0/1/2` 或 `replacement`)。

```json
{
  "texts": ["甲丙", "甲丁", "甲乙"],
  "decisions": [
    {"signature": [1, 1, 1], "source": 0}
  ]
}
```

```bash
curl -X POST http://localhost:8000/reconcile/adjudicate \
  -H "Content-Type: application/json" \
  -d '{"texts": ["甲丙", "甲丁", "甲乙"], "decisions": [{"signature": [1, 1, 1], "source": 0}]}'
```

响应示例:

```json
{
  "consensus": "甲丙",
  "total_cost": 3,
  "columns": [
    {
      "index": 0,
      "mask": 7,
      "cost": 0,
      "sources": [
        {"grapheme": "甲", "position": 0},
        {"grapheme": "甲", "position": 0},
        {"grapheme": "甲", "position": 0}
      ],
      "consensus": "甲",
      "anchored": false
    },
    {
      "index": 1,
      "mask": 7,
      "cost": 3,
      "sources": [
        {"grapheme": "丙", "position": 1},
        {"grapheme": "丁", "position": 1},
        {"grapheme": "乙", "position": 1}
      ],
      "consensus": "丙",
      "anchored": false
    }
  ],
  "uncertain": [],
  "audits": [
    {
      "signature": [1, 1, 1],
      "original": ["丙", "丁", "乙"],
      "adopted": "丙",
      "method": "source:0"
    }
  ]
}
```

使用人工替换字素、并对只消费单个来源的列(签名含 `null`)裁决的示例:

```bash
curl -X POST http://localhost:8000/reconcile/adjudicate \
  -H "Content-Type: application/json" \
  -d '{"texts": ["甲乙", "", ""], "decisions": [{"signature": [1, null, null], "replacement": "丁"}]}'
```

裁决失效时(下例中目标列不存在、裁决已确定列、重复裁决同时发生)返回 409 且无任何部分结果:

```json
{
  "detail": [
    {"decision": 0, "signature": [9, 9, 9], "reasons": ["坐标签名在重算后的对齐中不存在"]},
    {"decision": 1, "signature": [0, 0, 0], "reasons": ["目标列已达成共识,并非 uncertain 列"]},
    {"decision": 3, "signature": [1, 1, 1], "reasons": ["裁决重复:同一坐标签名被多次裁决"]}
  ]
}
```

### `POST /reconcile`

请求体为恰好三段文本,每段不超过 **120 个 Unicode 字素簇**(按 UAX #29 切分,如 `👨‍👩‍👧‍👦` 计 1 个;先 NFC 规范化再对齐):

```json
{
  "texts": ["甲丙", "甲丁", "甲乙"]
}
```

```bash
curl -X POST http://localhost:8000/reconcile \
  -H "Content-Type: application/json" \
  -d '{"texts": ["甲丙", "甲丁", "甲乙"]}'
```

响应示例:

```json
{
  "consensus": "甲�",
  "total_cost": 3,
  "columns": [
    {
      "index": 0,
      "mask": 7,
      "cost": 0,
      "sources": [
        {"grapheme": "甲", "position": 0},
        {"grapheme": "甲", "position": 0},
        {"grapheme": "甲", "position": 0}
      ],
      "consensus": "甲"
    },
    {
      "index": 1,
      "mask": 7,
      "cost": 3,
      "sources": [
        {"grapheme": "丙", "position": 1},
        {"grapheme": "丁", "position": 1},
        {"grapheme": "乙", "position": 1}
      ],
      "consensus": "�"
    }
  ],
  "uncertain": [
    {
      "column": 1,
      "sources": [
        {"grapheme": "丙", "position": 1},
        {"grapheme": "丁", "position": 1},
        {"grapheme": "乙", "position": 1}
      ]
    }
  ]
}
```

### `POST /reconcile/anchored`

与 `/reconcile` 相同,但可额外提供**锚点**:每个锚点给出三份文本各自的字素簇位置(0 起始),强制这三个字素出现在同一对齐列。对齐时以锚点切分前后区间,各区间分别求全局最小成本后拼接;锚点列照常参与成本与共识计算。响应每列增加 `anchored` 标记,所有 `position` 仍为原文绝对位置。

锚点需满足:各来源位置**严格递增**、**不越界**、**不重复**,否则返回 422;`anchors` 可省略或为空,此时等价于 `/reconcile`(仅多出 `anchored` 标记)。

```json
{
  "texts": ["甲乙", "甲丙", "甲乙"],
  "anchors": [{"positions": [1, 1, 1]}]
}
```

```bash
curl -X POST http://localhost:8000/reconcile/anchored \
  -H "Content-Type: application/json" \
  -d '{"texts": ["甲乙", "甲丙", "甲乙"], "anchors": [{"positions": [1, 1, 1]}]}'
```

响应示例(第二列为锚点强制对齐列):

```json
{
  "consensus": "甲乙",
  "total_cost": 2,
  "columns": [
    {
      "index": 0,
      "mask": 7,
      "cost": 0,
      "sources": [
        {"grapheme": "甲", "position": 0},
        {"grapheme": "甲", "position": 0},
        {"grapheme": "甲", "position": 0}
      ],
      "consensus": "甲",
      "anchored": false
    },
    {
      "index": 1,
      "mask": 7,
      "cost": 2,
      "sources": [
        {"grapheme": "乙", "position": 1},
        {"grapheme": "丙", "position": 1},
        {"grapheme": "乙", "position": 1}
      ],
      "consensus": "乙",
      "anchored": true
    }
  ],
  "uncertain": []
}
```

## 对齐规则

- 每列可消费任意非空来源组合,以位掩码表示:bit0/1/2 分别对应 `texts[0]/texts[1]/texts[2]`(如 `mask=5` 表示该列消费来源 0 与 2)。
- 单列成本:两两配对中,字符不等或字符与空位配对各计 1,空位与空位计 0;总成本为各列之和,取全局最小。
- 平局时依次偏好:消费来源数更多的掩码、位掩码更小的掩码(优先级 `7 > 3 > 5 > 6 > 1 > 2 > 4`)。
- 共识:某列至少两份相同非空字素时写入该字素,否则写入 `�`,并在 `uncertain` 中列出该列各来源的字素与原位置(0 起始,空位为 `null`)。
- 锚点(仅 `/reconcile/anchored` 与 `/reconcile/adjudicate`):每个锚点强制三份文本指定位置的字素同列(掩码 7);锚点把序列切分为若干区间,各区间独立求全局最小成本后拼接,锚点列照常计入成本与共识。
- 人工裁决(仅 `/reconcile/adjudicate`):以三来源原始位置签名定位 `uncertain` 列,采用某非空来源字素或单个 NFC 字素簇替换值;服务端重算对齐后一次性原子校验全部裁决,任一失效返回 409 且无部分结果,成功则返回裁决后文本、未裁决列与审计项。
- 持久化案件(`/cases` 系列):三段文本、锚点、裁决列表、当前结果与递增 `revision` 存入 SQLite;追加裁决须携带 `expected_revision`,服务端在同一事务中校验版本、合并既有与新增裁决并重算/原子校验,版本不符或任一裁决失效均返回 409 且不写入,成功后 `revision` 加一。
- 锚点建议(`GET /cases/{case_id}/anchor-suggestions`,只读):候选为三方一致、各自唯一且双向极大的连续公共片段,排除已保存锚点覆盖的片段;按长度降序、三方起点字典序排序后选出总覆盖字素数最大的互不重叠单调链(同分时依次取候选数更少、名次序列更小者);建议与既有锚点合并后各来源位置仍严格递增,不修改案件。

## 本地开发

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload        # http://localhost:8000
python -m pytest tests/              # 运行测试(含与纯 Python 参考实现的随机对拍)
```

案件数据库路径由环境变量 `CASE_DB_PATH` 指定,本地开发默认 `./data/cases.db`(已加入 `.gitignore`);Compose 部署时为 `/data/cases.db`,落在 `case_data` 数据卷中。
