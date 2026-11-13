# 示例清单（examples manifest）

配合 `demo.py` 演示共享查询、取消与失败语义。所有 CSV 均为"只有 `gene_id` 列"的上传格式；
标识末尾的 `.数字` 为测序软件导出的版本后缀，查询时会被去掉，输出中原标识照留。

| 文件 | 内容 | 用途 |
| --- | --- | --- |
| `batch_a.csv` | TP53(ENSG00000141510.17，含重复行)、BRCA1、EGFR、一个未知基因 ENSG00000999999.1 | 场景 1：与 B 并行提交，**保留**至完成；演示重复行/行序保留与 404 → `NA` |
| `batch_b.csv` | TP53(ENSG00000141510.11)、BRCA1、BRCA2、KRAS | 场景 1：与 A 共享 TP53/BRCA1 查询，运行中**被取消**；A 照常完成 |
| `batch_c.csv` | APOE、DMD | 场景 2：单独提交后立即取消（中止查询），再用同一文件立即重查（批次 D） |
| `batch_fail.csv` | TP53 + ENSG00000FAIL001.3 | 场景 3：源端对含 `FAIL` 的标识返回 500 → 整批失败、无下载 |
| `demo.py` | 标准库脚本 | 依次执行三个场景并校验源端 `/stats` 的请求计数 |

运行：

```bash
docker compose up --build        # 另开一个终端
python3 examples/demo.py
```

环境变量 `API_URL` / `SOURCE_URL` 可覆盖默认地址（`http://localhost:8000` / `http://localhost:8001`）。
