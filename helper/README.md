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
--beam-size 1            束搜索宽度，1 = 贪心解码（默认，最快）
--best-of 1              候选采样数，1 = 不做额外采样（默认）
--keep-temp              保留临时文件，调试用
```

### 速度

默认是贪心解码，实测比 5 束搜索快约 30%。而且**束搜索未必更准**：在测试素材上

```
5 束搜索： Eating is usually viewed as a self-care technique   ← 把自造词听成了 Eating
贪心解码： Bedroting is usually viewed as a self-care technique
```

遇到内容里的自造词、专有名词，束搜索反而容易"顺"成常见词。想要更保守的结果可以 `--beam-size 5 --best-of 5`，代价是慢三分之一。

实测（8 分 17 秒视频，base.en，6 核）：

| 阶段 | 耗时 |
|---|---|
| 取音频地址 + 下载 | 约 3 秒 |
| 转码 16kHz | 约 1 秒 |
| 转写（贪心） | 约 13 秒 |
| 翻译（并发 4 路均分） | 约 5 秒 |
| **合计** | **约 21 秒** |

同一个视频 + 同一套参数再生成一次会直接命中缓存（结果保留 30 分钟、最多 5 份），秒回。改了模型、语言、翻译开关或输出形式都会重新转写。

还想更快可以换量化模型，体积也小得多：

```bash
curl -L -o ~/.cache/whisper.cpp/ggml-base.en-q5_1.bin \
  https://hf-mirror.com/ggerganov/whisper.cpp/resolve/main/ggml-base.en-q5_1.bin
```

`q5_1` 是 57MB（f16 是 141MB），实测转写再快约 15%，识别结果与 f16 基本一致。把模型名换成它即可，或用 `--model` 指定。

线程数不用调：2/3/4/6 线程实测差在 15% 以内，默认取满 CPU 核数已经是最优。

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

| GET | `/health` | 依赖状态（whisper / ffmpeg / 模型）与运行时计数 |
| POST | `/jobs` | 建任务，返回 `{job_id}` |
| GET | `/jobs/<id>` | 轮询：`status` / `percent` / `message` / `cues` |
| DELETE | `/jobs/<id>` | 释放任务：跑着的会终止子进程，完成的会释放结果 |
| POST | `/cleanup` | 立即清理：释放过期结果 + 扫残留临时目录 |
| POST | `/transcribe` | 同步版本，阻塞到出结果 |

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

## 临时文件与清理

一次任务会在系统临时目录建一个 `boc-whisper-*` 工作目录，放音频、wav 和 whisper 的中间结果。清理策略：

| 时机 | 行为 |
|---|---|
| 任务结束（成功或失败） | 立刻删掉自己的 `boc-whisper-*` 目录 |
| 服务启动 | 扫一遍临时目录，删掉上次崩溃／被杀留下的残留（只删自己前缀的，且跳过 10 分钟内还热着的） |
| 服务退出（Ctrl-C / SIGTERM） | 先终止正在跑的 whisper 子进程（避免变孤儿继续占 CPU），再删掉所有本进程的临时目录 |
| 任务结果 | 完成的结果在内存里保留 30 分钟、最多 20 个，超时或超量自动释放；后台线程每分钟扫一次 |
| 转写结果缓存 | 同视频同参数的结果保留 30 分钟、最多 5 份，命中则直接复用，不重新转写 |
| 扩展取走结果 | 主动 `DELETE /jobs/<id>`，服务立即释放 |

`--keep-temp` 会关掉自动删除（调试用），此时目录路径会打进日志。想手动催一次清理：

```bash
curl -X POST http://127.0.0.1:8788/cleanup
```

`GET /health` 里有 `temp_dirs`（本进程还开着的工作目录数）和 `running_jobs`，正常空闲时应为 `0`。

## 开销参考

8 分 17 秒的视频（base.en / M 系列芯片）：音频下载约 2 秒，转码约 1 秒，转写约 20 秒，翻译约 25 秒。中间文件在临时目录，跑完即删。
