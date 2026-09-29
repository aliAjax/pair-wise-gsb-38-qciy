# 影视字幕本地化质检

一个仅使用 Python 标准库实现的字幕翻译、时间轴审核和交付服务。SQLite 保存项目、字幕版本、人员分配、时间点评论、术语表、复核意见和交付快照。

## 运行

```bash
python app.py --init
python app.py --port 8009
```

打开 <http://127.0.0.1:8009>。`--init` 会创建示例纪录片项目、`zh-CN` 草稿版本和一条术语规则。数据库默认是 `subtitle_qc.db`，可用 `--db` 或 `SUBTITLE_DB` 修改。

## 流程

1. 负责人创建项目、字幕版本和术语规则。
2. 为版本分配 `translator`、`timeline`、`reviewer`。
3. 翻译或时间轴成员保存字幕；每项包含 `expected_revision`，旧页面提交会返回 409。
4. 成员可对具体字幕或毫秒时间点添加评论。
5. 翻译/时间轴成员提交复核，分配的非创建人复核人批准或退回。
6. 负责人锁定已批准版本，再执行交付。
7. 交付时生成确定性的 SHA-256 快照；同语言的新交付会把旧版本标记为 `superseded`，但旧快照不会删除或覆盖。
8. 离线剪辑车批次回网时，携带基线修订号和操作日志上传，只生成三路合并结果与待处理清单；负责人逐项裁决后确认，单事务原子入库。

字幕保存会验证时长范围、起点小于终点、字幕重叠、序号冲突和术语表。术语表中配置的禁用译法会直接阻止保存；指定译法可用。

## 离线批次合并

译制组在离线剪辑车上基于某个基线修订号改字幕，回网后批次与主版本做**字段级三路合并**：

- 每条操作是 `upsert` 或 `delete`；针对已有字幕必须带 `base` 基线快照（四字段），新增字幕用 `client_key`。
- 只有一侧改过的字段自动取该侧；同一字段两侧都改成不同值时，两侧内容都保留在待处理清单（`field` 类型）里，由负责人选择 `main` / `offline` / `custom`。
- 主版本已删而离线仍编辑（或反向删除）生成 `identity` 身份冲突；时间轴交叉、序号碰撞生成 `timeline` 冲突；术语违规生成 `glossary` 冲突。导入阶段**绝不写主版本**。
- 导入失败（日志或基线不合法）整批不落库，可用同一 `batch_uid` 修正后重试；重复上传相同 `batch_uid` 直接返回原批次（200 而非 201），只结算一次。
- 确认时基于最新主版本重新计算合并并重放裁决；仍有未裁决项、裁决后仍违反术语表或时间轴，返回 409。
- 确认在单事务内完成：写入字幕、按实际改动条数递增 `revision`、重锚评论（被删字幕上的评论回落到覆盖该时间点的其他字幕，否则退化为纯时间点评论）、生成确定性 SHA-256 交付快照。仅 `draft` 版本可合并。
- 任何成员可 `GET` 差异和清单；只有项目负责人（或 `admin`）能裁决、确认，其他角色返回 403。

## API

所有身份通过 `X-User`、`X-Role` 请求头模拟，角色包括 `owner`、`admin`、`translator`、`reviewer`、`timeline`。

- `POST /api/projects`：创建项目和成片校验信息。
- `POST /api/projects/{id}/versions`：创建目标语言版本，可指定同语言父版本。
- `POST /api/projects/{id}/glossary`：设置指定译法和禁用词。
- `POST /api/versions/{id}/assignments`：分配角色。
- `POST /api/versions/{id}/cues`：新增或修改字幕，要求 `expected_revision`。
- `POST /api/versions/{id}/comments`：按具体时间毫秒或字幕 ID 评论。
- `POST /api/versions/{id}/submit|review|lock|deliver`：完成审核交付状态机。
- `GET /api/versions/{id}/cues|comments`、`GET /api/deliveries`：查看结果。
- `POST /api/versions/{id}/merges`：上传离线批次（`batch_uid`、`baseline_revision`、`operations`），重复批次返回 200。
- `GET /api/versions/{id}/merges`、`GET /api/merges/{id}`：查看批次差异与待处理清单（成员可读）。
- `POST /api/merge-items/{id}/resolve`：负责人裁决单项（`main` / `offline` / `custom`）。
- `POST /api/merges/{id}/confirm`：负责人确认，原子入库并生成快照。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖完整复核交付流程、锁定覆盖保护、旧修订冲突、时间轴重叠、术语禁用和人员权限。
