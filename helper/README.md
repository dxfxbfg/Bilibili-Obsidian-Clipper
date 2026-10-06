# 字幕助手服务

给扩展补上「B 站视频没有字幕轨时生成字幕」的能力。

浏览器扩展跑在沙箱里，不能执行本地程序，所以 whisper 转写放在这个本地服务里做：

```
扩展（无字幕时）→ POST /jobs → 助手服务
                                 ├─ playurl 取音频（未登录也能拿）
                                 ├─ ffmpeg 转 16kHz 单声道
                                 ├─ whisper-cli 转写
                                 └─ 可选：调翻译服务生成中文
扩展轮询 /jobs/<id> → 拿回字幕 → 预览 / 下载 / 写入 Obsidian
```

## 依赖

```bash
brew install whisper-cpp ffmpeg
mkdir -p ~/.cache/whisper.cpp
curl -L -o ~/.cache/whisper.cpp/ggml-base.en.bin \
  https://hf-mirror.com/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin
```

模型可选大小（越大越准越慢）：

| 模型 | 体积 | 说明 |
|---|---|---|
| `ggml-base.en.bin` | 141MB | 英文，够用 |
| `ggml-small.en.bin` | 466MB | 英文，更准 |
| `ggml-base.bin` | 141MB | 多语言（中日韩等要装这个） |

模型放 `~/.cache/whisper.cpp/` 下会被自动探测到；放别处用 `--model` 指定。

## 启动

```bash
python3 helper/boc_whisper_helper.py
```

常用参数：

```
--port 8788              监听端口，默认 8788
--model <路径>           指定模型，默认自动探测
--translate              默认开启翻译（也可以在扩展里控制）
--threads 8              whisper 线程数，默认取 CPU 核数
--keep-temp              保留临时文件，调试用
```

启动后访问 <http://127.0.0.1:8788/health> 应看到依赖状态。

### 开机自启（macOS）

```bash
cp helper/com.boc.whisper-helper.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.boc.whisper-helper.plist
```

停用 / 恢复：

```bash
launchctl bootout gui/$(id -u)/com.boc.whisper-helper
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.boc.whisper-helper.plist
```

日志在 `~/Library/Logs/boc-whisper-helper.log`。**plist 里的路径是本机绝对路径，换机器要改。**

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 依赖状态（whisper / ffmpeg / 模型） |
| POST | `/jobs` | 建任务，返回 `{job_id}` |
| GET | `/jobs/<id>` | 轮询：`status` / `percent` / `message` / `cues` |
| POST | `/transcribe` | 同步版本，阻塞到出结果 |
| DELETE | `/jobs/<id>` | 删任务 |

请求体：

```json
{
  "bvid": "BV17XVtzZEHn",
  "cid": "29830284611",
  "language": "en",
  "translate": true,
  "output": "bilingual"
}
```

- `language`：`en`（默认）/ `auto` / `zh` / `ja` …；非 `en` 需要多语言模型
- `output`：`bilingual`（译文在上、原文在下）/ `translated` / `original`
- `translate`：是否翻译，默认跟随服务启动参数
- `cid` 可省略，服务会自己查

命令行试一下：

```bash
curl -s -X POST http://127.0.0.1:8788/jobs \
  -H 'Content-Type: application/json' \
  -d '{"bvid":"BV17XVtzZEHn","translate":true}'
```

## 听写纠正

`text_fixes.json` 是术语纠正表，键为正则、值为替换文本，在翻译之前应用。whisper 遇到自造词或专有名词容易听错，按视频内容往里加就行。

## 翻译服务

默认用 pdf2zh-next 的免费代理 `api1.pdf2zh-next.com/chatproxy`。两点注意：

- 请求体必须是它认的官方模板格式，否则返回 `Keyword not allowed`
- 调用方要带浏览器 UA，`Python-urllib` 之类的会被 403

换别的服务用 `--translate-url`，只要兼容 `POST {"text": ...} → {"content": ...}`。

## 开销参考

8 分 17 秒的视频（base.en / M 系列芯片）：音频下载约 2 秒，转码约 1 秒，转写约 20 秒，翻译约 25 秒。中间文件在临时目录，跑完即删。
