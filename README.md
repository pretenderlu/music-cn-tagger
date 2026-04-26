# Music CN Tagger

把英文 / 拼音标签的中文音乐还原成中文 metadata —— 适合 Asian pop 音乐库被 ripping 工具或网盘同步「污染」成 `Yeh Hui-mei / Sunny Day` 这种英文转写后的清理场景。

> A web tool to retag transliterated / English-tagged Chinese music files back to their original Chinese metadata, via Apple iTunes & NetEase Cloud Music.

## ✨ 功能

- 🎵 扫描音乐目录（mp3 / flac / m4a / mp4 / ogg / opus / wav），按专辑批量识别
- 🔄 **两阶段匹配**：iTunes 翻译英文/拼音艺人专辑名 → NetEase / iTunes 中文区拿曲目数据
- 🎯 **拼音兜底**：iTunes 直搜失败时，artist → 列出 ta 全部专辑 → 拼音匹配（`Dan Dan You Qing` → 淡淡幽情）
- ✏️ **手动指定专辑**：填入已知的中文/英文专辑名，列出候选卡片让你点选确认
- 💾 **一键应用**：写 ID3 tag + 同步重命名文件（`05 - 晴天.mp3`）
- 🌐 实时进度日志、可编辑的结果表、置信度筛选
- 🔁 简繁转换（zhconv，TW 储存区拿到的繁体自动转简体）

## 📦 安装

需要 Python **3.10+**：

```bash
pip install -r requirements.txt
```

## 🚀 启动

**Windows**：双击 `start.bat`

**Linux / macOS**：

```bash
./start.sh
```

或直接 `python app.py`。启动后会自动打开 `http://localhost:5174`。

## 🎬 使用流程

### 自动扫描（标签较规范的音乐库）

1. 点「**浏览**」选音乐目录（每个子目录视为一张专辑）
2. 点「**开始自动扫描**」
3. 在结果表里检查、编辑、勾选要应用的行
4. 点「**应用到 N 个文件**」 → tag 写入 + 文件重命名

### 手动指定专辑（auto 失败 / 标签太烂）

1. 选目录（这个目录视为单张专辑）
2. 展开「**已知专辑/艺人名？**」
3. 填入专辑名 / 艺人名（中文最快，英文/拼音也支持）
4. 点「**搜索预览**」
5. 候选卡片中找到正确的，点「**✓ 就是这个**」
6. 自动按 tracknumber / 文件位置匹配本地文件

## ⚙️ 配置

界面右侧的选项：

| 项目 | 说明 |
|---|---|
| 数据源 | 上下箭头调整 NetEase / iTunes 优先顺序，默认 NetEase 优先（中文区有数据时优先用） |
| iTunes 储存区 | `tw` 推荐（华语 catalog 最全） / `cn` / `hk` / `jp` / `us` |
| 简繁转换 | 默认开启，TW 繁体 → 大陆简体 |
| 置信度阈值 | 低于此分数不自动勾选 apply（默认 0.6） |
| 投票曲数 | NetEase 兜底 song-vote 时用几首投票（默认 4） |
| 应用时同步重命名文件 | 默认开启，文件名变成 `NN - 新标题.ext` |

## 🧰 命令行用法（无 UI）

```bash
# 扫描，输出 CSV 到目录下
python tagger.py scan "E:/Music/某专辑"

# 应用 CSV（含重命名）
python tagger.py apply "E:/Music/某专辑/music_cn_suggestions.csv" --rename

# 仅预览不写入
python tagger.py apply "E:/Music/某专辑/music_cn_suggestions.csv" --dry-run
```

## 🌐 环境变量（可选）

适合放服务器上跑、给局域网内多设备访问：

| 变量 | 默认 | 说明 |
|---|---|---|
| `HOST` | `127.0.0.1` | 绑定地址，设 `0.0.0.0` 暴露到 LAN |
| `PORT` | `5174` | 端口 |
| `MUSIC_ROOT` | （空）| 限制目录浏览器只能在此子树内 |
| `OPEN_BROWSER` | `1` | 启动时是否自动打开浏览器 |

## 🛠 技术栈

- **后端**：Flask + mutagen + zhconv + pypinyin + requests
- **前端**：Tailwind CSS + Alpine.js（CDN，零构建）
- **数据源**：iTunes Search API（无需注册）+ NetEase Cloud Music API（非官方）

## ⚠️ 已知局限

- iTunes 数据源不覆盖**网络厂牌 / 独立单曲**，对小众作品命中率低
- NetEase 部分艺人专辑因版权撤掉了搜索索引（如周杰伦的部分专辑），但通过 album_id 直接拉曲目仍然可用
- 翻唱 / 同名异曲偶尔会带偏排序——所以**确认候选这一步不能省**
- iTunes Search API 有限速（约 20 req/s），扫超大库会慢

## 📝 License

MIT
