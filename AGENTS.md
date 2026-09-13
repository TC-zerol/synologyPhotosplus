# AGENTS.md — AI 快速上手指南

> 本文件面向 AI 助手/新接手的开发者。读完即可安全地修改本项目。
> 人类用户请读 README.md；本文只讲"你需要知道且容易踩坑"的部分。

## 1. 一句话定位

给 **Synology Photos 官方 App** 增加自定义智能打标：把 YOLO 物体检测、CLIP 语义标签、
RapidOCR 中文文字的识别结果，**直接写入群晖 Photos 的 PostgreSQL 内部数据库**，
使官方 App 的搜索框能搜到这些词。运行目标是群晖 DS220+（J4025 双核，无 AVX）的
Docker（Container Manager），Web 控制台端口 **47310**（host 网络）。

这属于"路线 A"（参考 eleonne/synology-tagger）：绕过官方 API 直写内部库。
**风险点：群晖升级 Photos 可能改表结构，SQL 需要跟着核对。**

## 2. 架构地图（改哪里找谁）

```
run.py                  入口：uvicorn app.main:app，端口 SP_PORT(默认47310)
app/
  main.py               FastAPI：全部 HTTP 接口、静态页、缩略图、备份/还原的后台线程
  pipeline.py           核心：任务调度、扫描流程、断点续扫、批量写库、容错
  dbaccess.py           synofoto 库访问：SSH→psql（默认）/TCP 直连两种传输；
                        全部 SQL 在这里生成（ENUM/写库/清理/备份还原）
  matcher.py            文件系统↔数据库 路径匹配（最长后缀 + 所属用户消歧）
  store.py              本地 SQLite（/config/tagger.db）：断点记账、写库台账、向量、日志
  config.py             /config/config.json 的默认值 + 深合并持久化
  vocab.json            CLIP 零样本词表（347 类 zh/en），用户可在网页编辑
  coco_zh.py            COCO-80 中文对照
  infer/
    registry.py         模型管理器：懒加载、空闲10分钟/任务结束自动卸载（内存释放）
    yolo.py             YOLOv8 ONNX 推理（letterbox+NMS 手写，无 torch 依赖）
    clip.py             CLIP ONNX 双塔：零样本打标 + 512维图像向量（语义搜图用）
    ocr.py              RapidOCR 封装 + 关键词提取（写入标签的词）
    video.py            视频抽帧 + 逐帧检测
    util.py             图片读取（含 HEIC/GIF 走 PIL）
  static/               前端（原生 JS，无框架无构建）。改前端后无需编译
models 已烘焙进镜像：yolov8n/s.onnx、clip/model.onnx(CLIP B/32 fp32)、
clip/cnclip.onnx(中文CLIP ViT-L int8，**默认**，配 cnclip_tokenizer.json；中文概念更准)。
**CLIP 档位由文件名识别**（`clip._profile`：文件名含 cnclip → 中文提示词
"一张{}的照片"/pad=0/text_len=52/768 维；否则标准 CLIP/英文提示词/pad=49407/77/512 维）。
向量按模型名存 `embeddings.model`，语义搜索只比对其后同一模型的向量；
切换 CLIP 模型需"重新分析(替换)"。单图标签总量上限 `tagging.max_tags_per_photo`
（默认 15）在 _analyze/_merge_backfill 强制。
```

数据流：`枚举 synofoto unit 表 → 文件索引&匹配 → 推理(结果先落本地SQLite)
→ 批量写 synofoto → 记账 written`。

## 3. 领域知识：群晖 Photos 数据库（必须懂）

- 库名：`synofoto`（团队空间）+ `synofoto_personal_<N>`（每个用户的个人空间）。
  `SELECT datname FROM pg_database WHERE datname LIKE 'synofoto%'` 枚举。
- 用到的表（其余几十张表**永远不要碰**）：
  - `unit`：id, filename, type(0图/1视频), createtime, mtime, id_folder
  - `folder`：id, name(路径形态因版本而异！), id_user
  - `user_info`：id, name（可能不存在，代码有降级 SQL `ENUM_SQL_NOUI`）
  - `general_tag`：id, id_user, name, count, normalized_name
  - `geocoding_info`：id_geocoding, lang, country/province/city/town（只读，供地点标签）
  - `many_unit_has_many_general_tag`：id_unit, id_general_tag
- **连接方式（默认 ssh）**：容器经 SSH 到 NAS，以 `sudo -S -p '' -u postgres psql`
  执行（root 登录则 `sudo -n -u postgres`）。DSM 的 psql 路径不定，`_probe_bin`
  探测多个候选。**不要**改 DSM 的 pg_hba/listen_addresses（那是老的 tcp 模式）。
- 搜索原理：Photos App 搜索按 `name`/`normalized_name` **前缀**匹配，
  所以标签要么是短词，要么把长词放进 normalized_name（我们存双语：
  normalized_name = "en zh" 或 "zh en" 小写）。
- `general_tag.id_user` 是标签的属主（按用户隔离）。我们用 unit 所属用户的
  id（`folder.id_user`），并以此保证搜索可见性。

## 4. 关键不变量（改代码前先背下来）

1. **先记账后写库**：每张图推理结果先写本地 SQLite（状态机
   `analyzed(待写库) → written / empty`；推理失败 `error`（≤3 次重试）；
   写库失败 `write_error`（保留结果，之后自动补写））。**任何情况下不要**
   在写库成功前删除本地记录。
2. **写库防重**：所有 INSERT 都带 `NOT EXISTS` 守卫；标签行 id 通过脚本
   **最后一个语句**的 `SELECT json_agg` 返回（psycopg2 只返回最后一个
   结果集——顺序不能动）。
3. **标签行的 created/复用语义**：写库前先查哪些 (id_user,name) 已存在。
   已存在的行是"复用"（可能是用户手工建的），记 `created=0`；
   我们新建的记 `created=1`。"重新分析(替换)"的清理脚本：
   created=1 的行**整行删除**；created=0 的行**只删我们建立的
   (unit,tag) 关联对**并修正 count，行本身与用户的其他关联**绝不删**。
   群晖自带的标签我们从未记账，天然不受影响。
4. **count 维护**：`general_tag.count` 在每批写库后按 (id_user,name) 精确重算，
   不要全表重算。
5. **配置快照**：任务启动时 `config.load()` 拍快照，运行中改配置不影响当前任务。
6. **engines 列**：processed 表记 `{"detect":bool,"clip":bool,"ocr":bool}`。
   状态是 written/empty 但有引擎为 False → 下次增量扫描**只重跑失败引擎**，
   新标签合并进旧标签（`_merge_backfill`）。
7. **model_version 指纹**（yolo 模型|clip 模型|ocr 开关|词表版本）变化 →
   那些照片视为"过期"，仪表盘提示条建议"重新分析(替换)"；增量扫描**不会**
   自动重刷过期照片（代价太大，必须用户显式操作）。
8. **内存**：10GB 内存，但要求用完释放——任务结束 `registry.unload_all()`，
   空闲 600s 自动卸载。新增模型务必走 registry.get()。
9. **单引擎容错**：_analyze 内 detect/clip/ocr 各自 try/except，日志记
   "XX 引擎失败（跳过该引擎）"，不让一个引擎挂掉拖垮整图。
10. **路径匹配不做绝对路径假设**：folder.name 形态随版本/空间类型变化。
    matcher 用"最长后缀 (≤8 级) + owner_name/Photos 前缀候选"消歧；
    跨用户同名目录（人人都有 MobileBackup）靠 owner 候选区分。
    歧义（match 返回 None）→ 记 error 跳过，绝不猜。
11. **写库目标库选择**：标签 id_user=unit 的 owner_id；TCP/SSH 两模式
    代码路径一致，只换传输层。

## 5. 常见修改的落点

| 想做什么 | 改哪里 |
|---|---|
| 加一个识别引擎 | `infer/` 新模块 → config.py 加开关 → pipeline._analyze 加分支(try/except+engines) → `_missing_engines`/`model_version` 纳入 → 前端设置页加控件 |
| 引擎说明 | detect/clip/ocr 走像素解码；exif 只读元数据（日期取 unit.takentime、地点取 geocoding_info 只读联查、相机取 EXIF 头），only={"exif"} 补全时零图片 IO |
| 历史照片补新引擎 | `_missing_engines` 规则：engines 键显式 False → 补；键不存在 → 仅 exif 视为待补（其他引擎视为旧版全成功），从而对存量照片做一次性轻量回填 |
| 改写库 SQL | 只改 `dbaccess.py`（注意不变量 2/3/4） |
| 加 Web 接口 | `main.py`；耗时操作必须用 `_bg_start` 后台线程 + `/api/bg/status` 轮询（模式照抄 backup/dbtest/restore），**别在 async 路由里同步长跑**（会卡死页面） |
| 改前端 | `static/`，原生 JS；`app.js` 的 `tick()` 2 秒轮询 `/api/status` |
| 加定时/调度逻辑 | `pipeline._scheduler`（每分钟检查，poll_interval_min>0 才自动增量） |

## 6. API 速查

`POST /api/login` · `GET /api/bootstrap`(全量) · `GET /api/status`(轻量轮询:
status/stats/stale) · `GET|POST /api/config` · `POST /api/job`
(type=incremental|replace(需 confirm:"REPLACE")|write_pending) ·
`POST /api/job/cancel` · `POST /api/db/test`(后台) · `POST /api/db/backup`(后台) ·
`GET /api/bg/status?kind=` · `GET /api/db/backups` ·
`POST /api/db/restore`({file, confirm:文件名}, 高危) · `PUT /api/vocab` ·
`POST /api/models/upload`(.onnx) · `GET /api/search?q=&mode=`(先 CLIP 语义后关键词) ·
`GET /api/file?path=`(缩略图,挂载根校验) · `GET /api/logs`。
鉴权：config.web.password 非空时需 cookie/X-Auth（依赖注入 `_auth`）。

## 7. 测试与验证

- `python tests/e2e_test.py`：**FakeTransport 模拟群晖库**（关键模式！
  所有管道回归都基于它，不碰真库），覆盖 扫描→断点→写库故障→补写→
  单引擎补全→语义搜索→API。
- 多用户消歧、清理脚本、匹配器都有独立单测片段（见对话历史/可重写）：
  核心断言是"created 行整删、reused 行只删自己的关联对"。
- 改完务必跑 e2e；涉及 SQL 的改动另写脚本级断言（看生成的 SQL 文本）。

## 8. 部署事实

- `docker compose up -d --build`；项目固定在 `/volume1/docker/synologyPhotosplus`，
  配置在项目内 `./config`（绑定挂载，目录已随项目附带 .gitkeep）。
- `network_mode: host` → 端口直接开在 NAS 上，无端口映射；SSH 目标 127.0.0.1。
- 照片目录只读挂载 `/volume1/homes:/photos/homes:ro`，容器自动递归索引
  `/photos` 全树（挂载源必须存在，否则容器起不来）。
- 模型源与校验（若需重下）：
  - yolov8n.onnx  sha256 013a98f3…  (HF: kshitijjjjjjjjjjjjjjjj/yolov8n-coco-onnx)
  - yolov8s.onnx  sha256 ae692a2c…  (HF: orirdx/yolov8s-coco-onnx → coco_yolov8s.onnx)
  - clip/cnclip.onnx (中文CLIP ViT-L int8, **唯一内置**) sha256 0898a3fa…待更新
    (HF: Xenova/chinese-clip-vit-large-patch14 → onnx/model_quantized.onnx，
     配 cnclip_tokenizer.json)
  - 旧 CLIP B/32 (model.onnx/model_quantized.onnx/tokenizer.json) 已删除省 760MB；
    配置若仍指向它们，registry.resolve_clip_model 会自动回退到 cnclip.onnx
  - 注意：SigLIP2 的 int8 导出用 ConvInteger 算子，onnxruntime CPU 不支持，勿选
- 依赖注意：`rapidocr-onnxruntime` 以 `--no-deps` 安装（防止拉入 GUI 版 opencv），
  其真实依赖手写在 requirements.txt（含 `tokenizers`——曾漏掉导致全量
  CLIP 失败，别再删）。

## 9. 运维速查

- 日志：网页"运行日志"页 或 `docker logs synology-photosplus`。
- 备份：`config/backups/<库名>_<时间>.sql.gz`（保留5份）；还原走网页
  （强确认）或 `gunzip -c xx.sql.gz | sudo -u postgres psql -d <库名>`。
- 完全重置：删 `config/config.json`（配置）/ `config/tagger.db`（断点，慎删）。

## 10. 已知限制（不要当成 bug 反复修）

- DSM 大版本升级可能改表结构 → 跑"测试连接"验证；
- 备份/还原/测试连接与扫描共享一条 SSH 连接（串行），备份期间扫描批次会等待；
- HEIC 在网页无法预览（识别不受影响）；网页无 HTTPS，仅限内网；
- Photos 搜索是标签前缀匹配，搜长句搜不到是官方行为；
- 替换模式无法区分"用户在我们打标后又手工贴的同名 AI 标签"（同一行，语义上归本工具管）。
