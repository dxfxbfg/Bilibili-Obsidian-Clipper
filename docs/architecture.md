# 项目架构与开发验证

这是一个不依赖前端构建框架的 Manifest V3 扩展，搭配两个可选的本地 Python 服务。源码目录 `extension/` 可直接加载；Firefox 通过发布脚本转换后台与侧栏配置。

## 运行边界

| 模块 | 职责 |
| --- | --- |
| `content.js` / `content.css` | B 站页面集成、视频与分 P 识别、字幕预览、阅读视图 |
| `popup.js` | 扩展弹窗中的字幕操作入口 |
| `background.js` | runtime 消息路由、设置与凭据存储、跨域请求、Obsidian 写入和 AI 请求编排 |
| `options.js` | Obsidian 目标、字幕助手、AI 平台等设置 |
| `sidepanel.js` | AI 聊天、历史对话和流式回答展示 |
| `ai/sse.js` | 独立于浏览器 API 的 OpenAI 兼容 SSE 解析器 |
| `ai/stream.js` | 解析器的 ESM 入口，供 `ai/client.js` 使用 |
| `boc_whisper_helper.py` | 下载音频、ffmpeg 转码、whisper 转写、翻译、缓存与任务查询 |
| `boc_native_host.py` | 浏览器 Native Messaging 与 Windows 桌面助手之间的启动桥梁 |
| `whisper-helper.js` | 助手健康检查、Native Messaging 按需启动、配置进度请求 |
| `boc_runtime.py` / `boc_gpu.py` | 硬件检测、校验下载、模型选择、GPU 推理和 CPU 回退 |
| `install_native_host.py` | Windows Chrome / Edge 的一次性当前用户注册 |
| `boc_desk_helper.py` | Windows 上打开 Obsidian 笔记并排列窗口 |

正常字幕路径：页面识别视频 → 扩展获取字幕轨与正文 → 预览、导出或通过 Obsidian Local REST API 写入笔记。

没有字幕轨时：扩展调用默认 `127.0.0.1:8788` 的 `/jobs` → 轮询任务状态 → 将生成的字幕接入相同的后续流程。助手用 `RUN_LOCK` 串行处理转写任务，避免同时运行多个转写进程。

AI 路径：侧栏通过 `sidepanel-chat` runtime port 发起请求 → 后台读取平台凭据并构造上下文 → 请求兼容的 `/chat/completions` → SSE 解析为 token → 通过 port 更新侧栏。生产后台当前使用 `background.js` 内的编排逻辑；`ai/client.js` 是另一套独立客户端，不能误认为修改它就会影响后台。

## SSE 解析器加载

- Chrome：保留 classic service worker，通过同步 `importScripts("ai/sse.js")` 加载。
- Firefox：打包后 `background.scripts` 按 `ai/sse.js`、`whisper-helper.js`、`background.js` 的顺序加载。
- ESM 客户端：通过 `ai/stream.js` 导出相同的解析器。

解析器处理 UTF-8 跨块、LF / CRLF / CR、多行事件、缺少结尾换行、`[DONE]` 与服务端错误。消费者提前结束、解析失败或请求中止时会释放 reader，避免流资源继续占用。整个模块只暴露一个 `BocAIStream` 命名空间。

## 本地配置与发布

项目不再预填或自动导入本机 Obsidian 仓库和密钥，请在扩展设置页自行配置。旧版的 `extension/targets.local.json` 自动导入逻辑已移除。发布脚本仍排除该旧文件、`.env*`、Python 缓存和 `.DS_Store`，避免残留本机文件被打包；两种浏览器使用相同的过滤规则。

Windows Native Messaging 的启动批处理通过自身所在目录定位脚本。默认从 PATH 调用 `python`，也可设置 `BOC_PYTHON` 指向 Python 可执行文件。桌面助手优先使用同一解释器目录下的 `pythonw.exe`，否则使用当前解释器。`helper/com.boc.desk.json` 的 `path` 和 `allowed_origins` 默认为空：使用前需填写本机批处理的绝对路径和实际扩展来源后注册；移动项目后也需更新注册配置。macOS 的 LaunchAgent 模板同样需替换脚本路径后安装。

## 验证命令

在项目根目录执行，需要 Node.js 18+ 和 Python 3.10+；测试不需要安装第三方依赖：

```sh
node --test tests/sse.test.cjs tests/whisper-helper.test.cjs
python -m unittest discover -s tests -p "test_*.py"
python scripts/build_release.py
```

自动测试覆盖 SSE 分块边界、流资源释放和两种发布包的配置隔离。发布测试在临时目录构造含模拟密钥的本机配置，并检查 zip 内的实际文件内容，而不仅检查忽略规则本身。

自动配置、硬件选择、校验下载、GPU 失败回退和取消后不重启任务也有回归测试。安装流程、下载来源和平台边界见[自动配置说明](auto-setup.md)。

浏览器内仍需手工验证：普通视频与分 P 字幕、无字幕任务、Obsidian 写入、侧栏生成和停止、Windows 保存并排。自动测试不代表这些外部服务与真实页面已完成联调。
