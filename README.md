<div align="center">

# Synology Photos+ 📸

**给群晖 Synology Photos 官方 App 加上自定义 AI 智能打标——物体识别、中文 OCR、语义标签，官方 App 搜索框直接可搜**

[English](README.en.md) · 简体中文

</div>

---

## ⚠️ 免责声明（使用前必读）

1. 本项目采用**非官方方式**，直接读写 Synology Photos 的内部 PostgreSQL 数据库（`general_tag` / `many_unit_has_many_general_tag` 等表）。该方式**未经群晖官方支持**，DSM/Photos 大版本升级可能改变表结构导致失效或异常。
2. 使用本项目即表示你理解并同意：**任何数据库层面的风险由你自行承担**。作者不对数据丢失、Photos 应用异常或任何间接损失负责。
3. 首次写库前请务必确认自动备份成功（`config/backups/`），并知晓还原方法。
4. 本项目仅供个人学习与自用，请勿用于商业用途（部分依赖的模型许可有限制，见"许可证"）。
5. 本项目与 Synology Inc. 无任何关联。

## 功能特性

| | |
|---|---|
| 🔍 **物体检测** | YOLOv8（ONNX，COCO 80 类，中英双语标签） |
| 🧠 **语义标签** | 中文 CLIP ViT-L 零样本打标，**466 类可编辑中文词表**（对齐华为图库等主流相册分类体系） |
| 📝 **中文 OCR** | RapidOCR（PP-OCRv4 ONNX）：发票号、单据、聊天截图全部可检索 |
| 🔎 **语义搜图** | 自然语言搜图（"海边日落""发票"），Immich 同款思路，基于 CLIP 图文向量 |
| 🖼 **结果预览** | 扫描进行中实时查看每张照片的标签与置信度百分比，边扫边调参 |
| ♻ **断点续扫** | 每张图结果先落本地 SQLite，断电/重启自动续接，绝不重复推理 |
| 🛡 **强容错** | 单图失败不中断（自动重试）；三引擎各自隔离、失败的引擎自动补跑；写库失败自动补写 |
| 🗄 **零侵入写库** | SSH → `sudo -u postgres psql`，**不修改 DSM 任何配置**；写库前自动 pg_dump 备份 |
| 🖥 **Web 控制台** | 仪表盘 / 识别设置 / 数据库诊断 / 模型与词表管理 / 备份还原 / 日志，原生 JS 无外部依赖 |
| 💤 **内存友好** | 任务结束与空闲 10 分钟自动卸载模型，10GB 内存 NAS 轻松运行 |

## 工作原理

```
照片目录(只读挂载) ──→ 枚举 synofoto 数据库 unit 表 + 文件索引路径匹配
        ──→ 推理：YOLOv8 物体 + 中文CLIP 语义标签 + RapidOCR 文字
        ──→ 结果先落本地 SQLite（断点）
        ──→ 批量写入 general_tag / many_unit_has_many_general_tag
        ──→ 官方 App 搜索框直接可搜（normalized_name 中英双语）
```

与 [eleonne/synology-tagger](https://github.com/eleonne/synology-tagger)（路线 A 验证者）原理相同，但完全重构：模型体积从 ~20GB 降到 ~1.5GB、写库通道零侵入、断点续扫、单引擎容错补全、Web 控制台。

## 部署（DS220+ 实测目标平台，x86_64 无 AVX 亦可运行）

```bash
# 1. 整个项目放到 /volume1/docker/synologyPhotosplus
# 2. 按需修改 docker-compose.yml 的照片挂载（源路径必须存在，否则容器起不来）
cd /volume1/docker/synologyPhotosplus
sudo chmod +x build.sh
sudo ./build.sh            # 干净的逐行构建输出（已禁用 buildkit 进度刷屏）
```

> 国内网络构建时模型下载慢/失败，加镜像源：
> `sudo ./build.sh --build-arg HF_ENDPOINT=https://hf-mirror.com`
>
> 首次克隆仓库构建会自动下载约 420MB 模型（带 sha256 校验）；
> 用整体文件夹拷贝部署的，确认 `app/models/` 里的 .onnx 已随拷贝到位——
> 可用 `python3 scripts/download_models.py --check --all` 逐文件诊断。

打开 `http://NAS_IP:47310`：

1. **数据库**页 → 填 SSH（主机 `127.0.0.1`、端口 22、**administrators 组账号**）→ "测试连接"应全绿
2. 确认挂载目录 ✔（个人空间挂 `/volume1/homes` 一行即可，多用户自动发现）
3. 建议先开 **Dry-run** 跑一批，到"结果预览"页调阈值/词表，满意后关 Dry-run 点"补写标签"
4. Synology Photos App 搜索框输入标签（如 `狗`、`发票`、`海边日落`）即可命中

## 数据安全

- 首次写库前自动对每个 `synofoto*` 库 `pg_dump` → `config/backups/`（保留 5 份），也可手动备份/页面还原（强确认）
- 只 `INSERT` 标签与关联、`UPDATE count`，不碰其他表；"重新分析(替换)"只删除**本工具创建**的标签，你手工打的标签不受影响
- 手动还原：`gunzip -c 备份文件.sql.gz | sudo -u postgres psql -d 库名`（先停容器）

## 常见问题

- **测试连接报"未找到 psql"**：SSH 账号须在 administrators 组；脚本会自动探测多个 psql 路径
- **弱标签太多（误报）**：调高"概率阈值"（建议 0.05~0.10）与"相似度下限"（0.22~0.28），配合"结果预览"的置信度百分比微调
- **想换识别类别**：直接在"模型与词表"页编辑（`中文,英文` 每行一类），新扫描立即生效，历史照片"重新分析"重刷
- **性能**：J4025 双核单张（三引擎）约 2~5 秒；`SP_ORT_THREADS=2`；夜间自动跑最舒适；无 AVX 也能跑（纯 onnxruntime 路线）

## 致谢与引用

本项目站在这些优秀项目的肩膀上：

| 项目 | 用途 / 许可 |
|---|---|
| [eleonne/synology-tagger](https://github.com/eleonne/synology-tagger) | 路线 A（直写数据库）的开创与验证 |
| [Ultralytics YOLOv8](https://github.com/ultralytics/ultralytics) | 物体检测模型（**AGPL-3.0**，权重衍生使用需遵循其许可） |
| [RapidOCR](https://github.com/RapidAI/RapidOCR) | 中文 OCR 引擎与模型（Apache-2.0） |
| [Chinese-CLIP](https://github.com/OFA-Sys/Chinese-CLIP)（阿里达摩院） | 中文图文对比学习模型（Apache-2.0），本项目主力识别引擎 |
| [OpenAI CLIP](https://github.com/openai/CLIP) | 零样本图文匹配范式（MIT） |
| [Xenova ONNX 转换](https://huggingface.co/Xenova) | CLIP / 中文 CLIP 的 ONNX 导出版本 |
| [onnxruntime](https://github.com/microsoft/onnxruntime) | CPU 推理运行时（MIT），无 AVX 可用 |
| [Tom Select](https://github.com/orchidjs/tom-select) | 前端下拉组件（Apache-2.0，已本地化） |
| [Immich](https://github.com/immich-app/immich) | 语义搜图的交互思路 |
| [FastAPI](https://github.com/tiangolo/fastapi) / [uvicorn](https://github.com/encode/uvicorn) | Web 框架（MIT / BSD） |
| 华为图库帮助文档 | 词表分类体系参考 |

## 许可证

本项目代码以 [MIT](LICENSE) 发布。**注意第三方模型许可**：YOLOv8 权重遵循 AGPL-3.0（商业使用需 Ultralytics 商业授权）；Chinese-CLIP、PP-OCR 模型为 Apache-2.0。详见 LICENSE 文件尾注。

---

<div align="center">

**English documentation → [README.en.md](README.en.md)**

</div>
