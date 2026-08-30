# Synology Photos+ 📸

给 **Synology Photos 官方 App** 增加自定义智能打标：物体识别、中文 OCR 文字检索、
CLIP 语义标签与自然语言搜图。识别结果直接写入 Synology Photos 的 PostgreSQL
（`general_tag` / `many_unit_has_many_general_tag`），**官方 App 的搜索框直接可搜**。

> 原理与 [eleonne/synology-tagger](https://github.com/eleonne/synology-tagger) 相同（路线 A），
> 但做了全面重构：更安全的写库通道、更轻的模型、断点续扫、Web 控制台。
> **AI 助手/贡献者请先读 [AGENTS.md](AGENTS.md)**（架构、领域知识、关键不变量与修改指引）。

---

## 与 synology-tagger 的区别

| | synology-tagger | 本项目 |
|---|---|---|
| 运行位置 | 外部机器 + SSH 挂载 | **NAS 本机 Docker**（DS220+ 实测目标平台） |
| 模型环境 | mmdetection + Torch，约 20GB | onnxruntime 全家桶，约 1.5GB |
| 识别能力 | COCO 80 类 | YOLO 80 类 + **CLIP 零样本 400+ 类（词表可编辑）** + **中文 OCR** |
| 写库通道 | 改 DSM 的 pg_hba/监听地址，开远程 PG | **SSH → `sudo -u postgres psql`，零改动 DSM 配置** |
| 容错 | 无断点 | **断点续扫**：每图结果先落本地 SQLite，写库失败自动补写，单图失败不中断 |
| 内存 | 常驻 | 任务结束/空闲 10 分钟**自动卸载模型释放内存** |
| 管理 | 简单网页 | 现代化控制台：进度、设置、模型更换、词表编辑、语义搜图、日志、备份 |

## 功能

- **物体检测**（YOLOv8n/s ONNX，COCO 80 类，中英双语标签）
- **CLIP 零样本打标**（ViT-B/32，内置约 400 类中文词表：场景/动物品种/食物/活动/节日/截图单据类型…，网页上随时增删类别）
- **中文 OCR**（RapidOCR / PP-OCRv4 ONNX）：发票号、单据、聊天截图等文字可检索；全文保留在本地可搜
- **语义搜图**（Immich 同思路）：本控制台内用自然语句搜图（"海边日落"），基于 CLIP 向量
- **增量轮询**：新照片自动打标（间隔可配）
- **安全写库**：写库前自动 `pg_dump` 备份（保留 5 份）；Dry-run 模式；一键补写
- **断点续扫与强容错**：任何时刻重启/断电，增量扫描自动续接，绝不重复推理

## DS220+ 部署步骤

### 0. 准备

1. DSM 套件中心安装 **Container Manager**（原 Docker）。
2. DSM 控制面板 → 终端机和 SNMP → **启用 SSH**。
3. 把本工程整个放到 `/volume1/docker/synologyPhotosplus`（与 compose 中的
   说明一致；所有数据都会保存在该目录的 `config/` 子目录里，不在其他位置创建文件）。

配置、记账库和数据库备份位于 `/volume1/docker/synologyPhotosplus/config/`：
`config.json`（配置）、`tagger.db`（记账）、`backups/`（pg_dump 备份）、
`models/`（自定义模型）、`vocab.json`（词表覆盖）。

### 1. 修改 docker-compose.yml

容器会**自动递归索引 `/photos` 下全部媒体文件**，多用户、MobileBackup、
PhotoLibrary 等子目录都会被自动发现，无需逐个配置：

- 用了**个人空间**（我的照片在 `~/Photos`）：挂 `/volume1/homes` 一行即可，
  所有用户的 `Photos/MobileBackup`、`Photos/PhotoLibrary` 全部覆盖；
- 还用了**团队空间**等其他共享文件夹：再加一行挂载（如 `/volume1/photo:/photos/photo:ro`）。

```yaml
    volumes:
      - ./config:/config                       # 配置（项目目录内，自动创建）
      - /volume1/homes:/photos/homes:ro        # 个人空间（多用户自动检测）
      # - /volume1/photo:/photos/photo:ro      # 团队空间（按需）
```

> 注意：照片目录等**绑定挂载**的源路径必须真实存在，否则容器无法启动。
> `config/` 目录已随项目附带（含 .gitkeep 占位文件），整体复制即可。

同名目录跨用户（如每个用户都有 `MobileBackup`）由匹配器结合数据库中的
所属用户自动消歧，不会串。

### 2. 构建并启动

整个项目文件夹复制到 NAS（`config/` 目录已随项目附带，无需手动创建），然后：

```bash
cd /volume1/docker/synologyPhotosplus
sudo docker compose up -d --build
```

（或在 Container Manager → 项目 → 新建，指向该目录；项目名/目录名任意。）

### 3. Web 控制台配置

浏览器打开 `http://NAS_IP:47310`（端口不常用，避免与常见服务冲突；可在
docker-compose.yml 中通过 `SP_PORT` 环境变量修改）：

1. **数据库**页：填 SSH 信息
   - 主机 `127.0.0.1`（host 网络下即 NAS 本机），端口 `22`
   - 用户：** administrators 组的 DSM 账号**（密码即 DSM 密码）
   - 点 **测试连接**：应依次通过"枚举 synofoto 数据库 → 读取 unit 表 → 文件系统索引"
2. **数据库**页：确认挂载目录状态为 ✔
3. **识别设置**页：按需开关 YOLO/CLIP/OCR/视频，选标签语言
4. 仪表盘点 **▶ 立即扫描**（建议先开 **Dry-run** 试跑一遍）

### 4. 验证

扫描若干照片后，打开 Synology Photos App，在搜索框输入标签
（如 `狗`、`蛋糕`、`发票`、`柯基`）即可命中。本控制台"图片搜索"页还可用
自然语句做语义搜图。

## 配置文件

`/config/config.json`（网页修改即可，无需手编），要点：

| 键 | 说明 |
|---|---|
| `db.transport` | `ssh`（默认，零侵入）或 `tcp`（synology-tagger 老路，需自行开 PG 远程） |
| `tagging.dry_run` | true = 只分析不写库 |
| `tagging.tag_prefix` | 标签前缀，如 `AI·`，便于和手工标签区分 |
| `tagging.language` | 标签显示语言 `bilingual/zh/en`（搜索走 normalized_name，双语都搜得到） |
| `scan.poll_interval_min` | 增量轮询分钟数，0=只手动 |
| `detect/clip/ocr/video.*` | 各引擎开关与阈值 |

CLIP 词表可在网页编辑（保存到 `/config/vocab.json`）。自定义 YOLO ONNX
在"模型与词表"页上传到 `/config/models/`。

## 数据安全（务必阅读）

- **备份**：第一次写库前会自动把涉及的每个 `synofoto*` 库 `pg_dump` 备份到
  `config/backups/`（保留 5 份），也可在"数据库"页手动"立即备份"
  （后台执行，完成后页面有提示）。文件位置：
  `/volume1/docker/synologyPhotosplus/config/backups/<库名>_<时间>.sql.gz`
- **还原**（SSH 到 NAS 执行；还原会覆盖该库现有数据，先确认）：

  ```bash
  # 团队空间库
  gunzip -c /volume1/docker/synologyPhotosplus/config/backups/synofoto_YYYYmmdd_HHMMSS.sql.gz \
    | sudo -u postgres psql -d synofoto
  # 个人空间库（库名见 config/backups/ 下的备份文件）
  gunzip -c .../synofoto_personal_2_xxx.sql.gz | sudo -u postgres psql -d synofoto_personal_2
  ```

  建议还原前先停容器（`docker compose stop`），还原完再 `start`。
- 本工具只 `INSERT` 标签与关联、`UPDATE count`，不碰其他表；
  重新分析(替换)只会删除**本工具自己创建**的标签行（本地有记账）。
- 风险提示：群晖升级 Photos 包可能变更库结构，写库逻辑需相应调整；
  出问题用上面的备份恢复。
- DSM 升级/Photos 重建索引不影响已写标签；删除照片时其标签关联由 Photos 自行处理。

## 断点与容错机制

- 每张图推理完成后立即写入本地 SQLite（`/config/tagger.db`），状态机：
  `analyzed（已分析待写库）→ written / empty`，失败为 `error`（自动重试 3 次）、
  `write_error`（写库失败，下次扫描/补写任务自动落库，不重复推理）。
- 任意时刻断电/重启：增量扫描自动从检查点继续。
- 任务结束与空闲 10 分钟后自动卸载全部模型会话，释放内存。

## 常见问题

- **测试连接报"未找到 psql"**：DSM 7 自带 PostgreSQL 工具在非常规路径，
  脚本已探测多个位置；仍失败请在 NAS 上 `sudo -u postgres psql -c 'select 1'`
  验证 SSH 用户 sudo 可用（账户须在 administrators 组）。
- **搜不到中文标签**：Photos 搜索对标签做前缀匹配；OCR 关键词已是分词粒度，
  尽量搜短词。normalized_name 中英混存，英文关键词也可命中。
- **J4025 没有 AVX**：本项目的 onnxruntime 路线不依赖 AVX，可放心运行。
- **速度**：DS220+ 双核单张图（检测+CLIP+OCR）约 3~8 秒，1 万张约一晚，
  增量扫描无感。建议 `SP_ORT_THREADS=2` 并让 NAS 夜间慢慢跑。

## 目录结构

```
├── AGENTS.md               # AI 助手快速上手指南（架构/不变量/修改指引）
├── docker-compose.yml      # 部署入口（照片挂载按需修改）
├── Dockerfile
├── run.py
└── app/
    ├── main.py             # FastAPI：配置/任务/诊断/搜索/缩略图
    ├── pipeline.py         # 任务调度、断点续扫、批量写库、容错
    ├── dbaccess.py         # synofoto 库访问（SSH→psql / TCP）
    ├── matcher.py          # 文件↔unit 后缀路径匹配
    ├── store.py            # 本地 SQLite 记账（检查点/向量/日志）
    ├── config.py           # 配置持久化
    ├── infer/              # yolo / clip / ocr / video + 模型管理器
    ├── models/             # 内置模型（yolov8n/s, CLIP, 已烘焙进镜像）
    ├── vocab.json          # CLIP 词表（400+ 类中英）
    └── static/             # Web 控制台（原生 JS，无外部依赖）
```

## 致谢

[eleonne/synology-tagger](https://github.com/eleonne/synology-tagger)（路线 A 验证者）·
[RapidOCR](https://github.com/RapidAI/RapidOCR) ·
[Ultralytics YOLOv8](https://github.com/ultralytics/ultralytics) ·
[OpenAI CLIP](https://github.com/openai/CLIP) ·
[Immich](https://immich.app)（语义搜索思路）

⚠️ 免责声明：本项目直接操作 Synology Photos 内部数据库，属于非官方用法，
使用前请做好备份，风险自担。
