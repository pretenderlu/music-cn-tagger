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

需要 **Python 3.10+**。

### Windows

1. 装 [Python 3.12 from python.org](https://www.python.org/downloads/windows/)（安装时勾选 *Add Python to PATH*；tkinter 和 pip 默认带）
2. 在项目目录开 PowerShell / CMD：
   ```bat
   pip install -r requirements.txt
   ```
3. 双击 `start.vbs`（**静默启动**，无控制台窗口，日常使用推荐）
   或 `start.bat`（**调试启动**，保留命令行窗口看日志/报错）

> 静默模式下要停止服务：任务管理器里 kill `pythonw.exe`（或浏览器关掉、命令行 `taskkill /im pythonw.exe`）

### macOS

1. 装 Python（推荐 [python.org installer](https://www.python.org/downloads/macos/)，自带 tkinter；用 Homebrew 装的话需要额外 `brew install python-tk`，否则原生文件夹选择器会回落到浏览器内 modal）
2. 终端 `cd` 到项目目录：
   ```bash
   pip3 install -r requirements.txt
   chmod +x start.sh   # 第一次需要赋执行权
   ./start.sh
   ```
3. 首次跑可能弹「无法验证开发者」——`系统设置 → 隐私与安全性` 里点「仍要打开」即可（这是因为 mutagen 等依赖里有未签名的二进制扩展）

### Linux

```bash
sudo apt install python3-tk           # tkinter 通常要单独装
pip install -r requirements.txt
./start.sh
```

### 通用

也可以直接 `python app.py` / `python3 app.py`。启动后浏览器自动打开 `http://localhost:5174`。

第一次自动扫描会创建 `~/.music-cn-tagger/cache.db` 缓存 MusicBrainz / Wikidata 查询结果（30 天 TTL，可随时删除重建）。

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
| `MUSIC_CN_TAGGER_CACHE` | `~/.music-cn-tagger/cache.db` | 自定义百科查询缓存路径 |

## 🛠 技术栈

- **后端**：Flask + mutagen + zhconv + pypinyin + requests + sqlite3
- **前端**：Tailwind CSS + Alpine.js（CDN，零构建）
- **数据源**（按解析阶段）：
  - **Stage 0 实体解析**：MusicBrainz API（`musicbrainz.org`，1 req/s 节流）+ Wikidata（`wikidata.org`，SPARQL 查专辑）—— 把英文/拼音艺人/专辑名解析到中文官名
  - **Stage 1 翻译兜底**：iTunes Search API 自带的英文别名索引
  - **Stage 2 曲目数据**：NetEase Cloud Music API（非官方）+ iTunes Search API
- **跨平台**：纯 Python，Windows / macOS / Linux 同一份源码

## ⚠️ 已知局限

- MusicBrainz API 在国内偶发 TLS 不通，已加重试；连续失败会自动 fall through 到 Wikidata 路径
- iTunes 数据源不覆盖**网络厂牌 / 独立单曲**，对小众作品命中率低
- NetEase 部分艺人专辑因版权撤掉了搜索索引（如周杰伦的部分专辑），但通过 album_id 直接拉曲目仍然可用
- 翻唱 / 同名异曲偶尔会带偏排序——所以**确认候选这一步不能省**
- iTunes Search API 有限速（约 20 req/s），扫超大库会慢

## 📝 License

MIT
