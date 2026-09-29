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

## 离线批次回网合并

离线剪辑车在无网环境下改完字幕后，把批次回网与主版本合并。每个批次携带基线版本 `baseline_revision` 和操作日志 `operations`（`add`/`update`/`delete`，用 `ref` 指向基线字幕）。

1. `POST /api/versions/{id}/offline-batches` 上传批次。系统按 `基线 × 离线 × 主版本` 做三方字段级合并：同一字幕两侧都改过时，不冲突的字段各自保留，冲突字段（`field`）以及术语冲突（`glossary`）、时间轴交叉（`timeline`）、一侧删除一侧修改（`delete`）进入待处理清单，**不直接覆盖主版本**。
2. 批次导入失败会标记为 `failed`，可通过 `POST /api/offline-batches/{id}/retry` 重试；同一内容重复上传只结算一次（幂等）。
3. `GET /api/versions/{id}/offline-batches` 与 `GET /api/offline-batches/{id}` 查看批次与差异；项目成员都能看差异，但只有负责人能确认。
4. 负责人确认时可在 `resolutions` 中逐条选择 `main`/`offline`（字段/删除/新增冲突）或 `accept`（术语/时间轴）；未选择时默认保留主版本。确认后原子入库：更新字幕、按身份重定位评论（删除的字幕评论脱离关联、改号的字幕评论跟随）、同步交付快照，版本修订号递增。

上传批次可附带 `baseline_cues`（剪辑车同步时的基线快照）；省略且 `baseline_revision` 等于当前修订号时，以主版本当前字幕为基线。

字幕保存会验证时长范围、起点小于终点、字幕重叠、序号冲突和术语表。术语表中配置的禁用译法会直接阻止保存；指定译法可用。

## API

所有身份通过 `X-User`、`X-Role` 请求头模拟，角色包括 `owner`、`admin`、`translator`、`reviewer`、`timeline`。

- `POST /api/projects`：创建项目和成片校验信息。
- `POST /api/projects/{id}/versions`：创建目标语言版本，可指定同语言父版本。
- `POST /api/projects/{id}/glossary`：设置指定译法和禁用词。
- `POST /api/versions/{id}/assignments`：分配角色。
- `POST /api/versions/{id}/cues`：新增或修改字幕，要求 `expected_revision`。
- `POST /api/versions/{id}/comments`：按具体时间毫秒或字幕 ID 评论。
- `POST /api/versions/{id}/submit|review|lock|deliver`：完成审核交付状态机。
- `POST /api/versions/{id}/offline-batches`：上传离线批次（基线版本 + 操作日志），返回三方合并结果与待处理清单。
- `GET /api/versions/{id}/offline-batches`、`GET /api/offline-batches/{id}`：查看批次与差异。
- `POST /api/offline-batches/{id}/confirm|retry`：负责人确认原子入库；失败批次重试。
- `GET /api/versions/{id}/cues|comments`、`GET /api/deliveries`：查看结果。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖完整复核交付流程、锁定覆盖保护、旧修订冲突、时间轴重叠、术语禁用和人员权限。
