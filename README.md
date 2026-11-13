# 基因注释对齐服务（Gene Annotation Alignment）

生物信息数据员在合并测序表达量结果前，用本服务把测序软件导出的 ENSG 标识对齐到固定的
人类基因注释版本：上传只含 `gene_id` 列的 CSV，服务**异步**为每行补入标准基因符号
（symbol）和染色体（chromosome），**保留原始标识、原始行序和重复行**。

## 组成

| 目录 | 说明 |
| --- | --- |
| `api/` | FastAPI 注释 API（批次管理、共享查询合并、取消、进度、下载） |
| `source/` | 本地注释源：模拟响应缓慢的 Ensembl 式查询服务，内置固定注释版本（GRCh38.p14 / Ensembl 112 演示子集），并统计每个基因收到的 HTTP 请求数 |
| `examples/` | 示例清单（CSV + `demo.py`），见 `examples/README.md` |
| `tests/` | 共享查询管理器的单元测试 |

## 快速开始

```bash
docker compose up --build
```

* API Swagger UI: <http://localhost:8000/docs>（注释源文档: <http://localhost:8001/docs>）
* 一键演示（另开终端）：`python3 examples/demo.py`

本地无 Docker 运行：

```bash
pip install -r api/requirements.txt -r source/requirements.txt
(cd source && uvicorn app.main:app --port 8001) &
(cd api && ANNOTATION_SOURCE_URL=http://localhost:8001 uvicorn app.main:app --port 8000) &
python3 examples/demo.py
```

## API 一览

| 方法与路径 | 说明 |
| --- | --- |
| `POST /batches` | 上传 CSV（`multipart` 文件或 `text/csv` 裸 body），创建异步批次，返回 202 |
| `GET /batches` / `GET /batches/{id}` | 批次列表 / 进度（`resolved_genes`/`unique_genes`、匹配数等） |
| `POST /batches/{id}/cancel` | 取消运行中的批次，**立即生效**；已结束批次返回 409 |
| `GET /batches/{id}/download` | 完成后下载 `gene_id,symbol,chromosome` CSV；失败/取消的批次返回 409（**不提供下载**） |
| `GET /debug/inflight` | 当前在途共享查询及其订阅批次（可观测性） |

注释源：`GET /annotations/{gene_id}`（200 / 404 / 500）、`GET /stats`、
`POST /stats/reset`（按基因统计收到的请求数，用于验证"同基因只发一次请求"）。

## 关键语义

* **版本后缀**：查询时去掉标识末尾的 `.<数字>`（如 `ENSG00000141510.17` →
  `ENSG00000141510`）；输出中**原标识照留**。
* **404 → 未匹配**：该行 symbol/chromosome 标为 `NA`，批次照常完成。
* **其他源端失败 → 整批失败**：5xx、超时、连接错误、响应格式非法都会使整批
  失败且不提供下载。
* **共享查询**：同一基因的在途查询全服务只发一次 HTTP 请求，各批次共享结果；
  源端 `/stats` 可验证。
* **取消**：立即生效；仍被其他批次需要的查询照常完成；没有批次再需要时立即
  中止 HTTP 请求。
* **不缓存**：已结束的查询不留存，新批次对同一基因必然另发请求。
* **迟到结果隔离**：被中止的旧请求若迟到完成，既不会覆盖新查询的注册项
  （按对象身份校验），也不会投递给任何批次；已取消的批次状态不可逆。
* **只存内存**：批次与在途查询均在单进程内存中（单 worker 运行），重启即清空。

## 演示场景（examples/demo.py）

1. **共享查询时取消一批**：A、B 两批并行，TP53/BRCA1 共享一次查询；取消 B 后
   A 照常完成，源端每个共享基因只收到 1 次请求。
2. **全部取消后立即重查**：取消 C（中止其查询）→ 立即提交同基因的 D → 源端
   对每个基因收到 2 次请求（1 次被中止 + 1 次新请求），D 正常完成。
3. **源端 500 → 整批失败**：含 `FAIL` 的标识触发源端 500，整批失败、无下载。

## 配置（环境变量）

| 变量 | 服务 | 默认 | 说明 |
| --- | --- | --- | --- |
| `ANNOTATION_SOURCE_URL` | api | `http://localhost:8001`（compose 中为 `http://source:8000`） | 注释源地址 |
| `SOURCE_TIMEOUT_SECONDS` | api | `30` | 源端请求超时（超时 → 整批失败） |
| `MAX_CONCURRENT_QUERIES` | api | `32` | 并发查询上限（信号量） |
| `ANNOTATION_DELAY_SECONDS` | source | `2.0` | 模拟源端迟缓的响应延迟 |

## 测试

```bash
python3 -m pytest tests/ -q     # 9 个单元测试：合并、中止、无缓存、迟到隔离、错误扇出等
python3 examples/demo.py        # 端到端演示（需服务已启动）
```
