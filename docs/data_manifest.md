# 数据清单（Data Manifest）

> 本文件由 `scripts/download_data.py` 自动生成，请勿手工编辑。

- 生成时间：2026-11-02 14:24:05 CST
- 数据来源仓库：https://github.com/tivonio/sql-churn-analysis
- 数据来源页面：https://www.kaggle.com/datasets/rivalytics/saas-subscription-and-churn-analytics-dataset
- 数据性质：**公开合成 SAS 数据（非真实企业数据）**
- 分发说明：上游仓库未声明 LICENSE，故原始 CSV 不进入本仓库，由脚本重建。

## 文件清单

| 表 | 文件 | 行数（不含表头） | 列数 | 字节数 | 状态 |
|---|---|---:|---:|---:|---|
| accounts | `ravenstack_accounts.csv` | 500 | 10 | 36,148 | 已存在 |
| subscriptions | `ravenstack_subscriptions.csv` | 5,000 | 14 | 432,565 | 已存在 |
| feature_usage | `ravenstack_feature_usage.csv` | 25,000 | 8 | 1,375,897 | 已存在 |
| support_tickets | `ravenstack_support_tickets.csv` | 2,000 | 9 | 143,597 | 已存在 |
| churn_events | `ravenstack_churn_events.csv` | 600 | 9 | 44,029 | 已存在 |

## 校验和（SHA-256）

| 文件 | SHA-256 |
|---|---|
| `ravenstack_accounts.csv` | `301318d5b68ca777803ab934432dbcc1ce92323f65a59117dcd5d6f6c1fc974a` |
| `ravenstack_subscriptions.csv` | `6d77c36b9f725214d3e560d54f7b0af7990b9bda2165b9f4393c04ef502c9c3c` |
| `ravenstack_feature_usage.csv` | `ef9a2ba1ae581ed9ab3b4b6886c4f25373984f47ed2a172c494b42a60d45e104` |
| `ravenstack_support_tickets.csv` | `57fb4812b933df080ff1b389e76f0b566b6db005fd4f2e59fda2d05b7dafb55e` |
| `ravenstack_churn_events.csv` | `b532bb4e00af480f53e0d373669af6e5a94487b91365be123f4222b31b197c9e` |

## 复核方式

```bash
shasum -a 256 data/raw/*.csv
```

若校验和与上表不一致，说明文件被改动或上游已更新，需重新执行 `python scripts/download_data.py --force` 并更新本文件。
