# 字幕助手自动配置与 GPU 优先

## 首次使用（Windows x64，Chrome / Edge）

1. 安装 Python 3.10+。下载项目源码或发布包中的 `*-helper.zip`，解压到长期保留的目录。
2. 加载或更新扩展，进入扩展设置，找到「无字幕视频：生成字幕」。
3. 在解压目录打开终端，执行设置页显示的命令：

   ```powershell
   python helper/install_native_host.py --extension-id 实际扩展ID --browser chrome
   ```

   Edge 将最后的 `chrome` 改成 `edge`。扩展 ID 可直接从设置页显示的命令获取。脚本使用当前 Python，在当前用户的注册表中登记 Native Messaging 启动器，无需管理员权限。注册内容只允许指定扩展调用；不要把示例文字当作真实 ID。

4. 回到扩展点击「自动配置字幕助手与模型」，查看检测、下载和就绪状态。也可以直接打开视频生成字幕：首次任务会完成相同的准备步骤，并在任务进度中显示下载量。

这是浏览器沙箱要求的一次本地安装；扩展不能绕过它自行运行 Python。移动助手目录、卸载原 Python 或更换扩展 ID 后，重新执行注册命令。请用新版本 helper 替换旧文件；旧版监听在 8788 时应先停止旧服务。

## 自动选择策略

- 通过 Vulkan 驱动枚举实际可用的 Windows GPU，不依赖 `nvidia-smi`，兼容支持 Vulkan 的 Intel、AMD 和 NVIDIA 显卡。
- 检测到 GPU 后优先下载并使用 Vulkan 转写运行时。模型加载或推理失败、驱动不兼容、下载失败时使用 CPU，并显示回退原因。每次转写读取引擎日志确认实际 GPU 后端，不能把“有显卡”当作“已使用显卡”。
- GPU 至少约 2 GB 可用容量且主机可用内存至少 3 GB 时选择 `small-q5_1`；约 1 GB 容量、主机可用内存至少 2 GB 时选择 `base-q5_1`；更紧张时选择 `tiny-q5_1`。集成显卡将容量限制为主机可用内存的一半，避免将共享内存全当独立显存。显卡报告的是总容量，实际资源争用由加载失败后的 CPU 回退处理。
- 没有可用 GPU 时，按 CPU 核数和内存选择：8 线程、约 16 GB 总内存且 6 GB 可用时选择 small；4 线程、约 8 GB 总内存且 2 GB 可用时选择 base；否则 tiny。
- 三种模型均为多语言量化模型，大小约 31 / 57 / 182 MiB。模型路径留空自动选择；填入完整路径则使用指定模型，不覆盖或删除该模型。
- 模型在实际转写时加载到内存 / GPU；GPU 进程在每个任务结束后关闭，释放显存。准备完成不代表后台永久占用显存。

## 下载、缓存与来源

Windows 缓存目录为 `%LOCALAPPDATA%\BOC\whisper`。依赖与模型下载到 `.part` 文件，通过固定 SHA-256 校验后才启用；下载失败不会将半个文件视为可用模型。重试会重新下载未完成文件，已验证完成的模型会复用。不会下载 GPU 驱动、CUDA Toolkit，也不会更改系统 PATH。

- CPU 引擎：[whisper.cpp 官方 b5454 发布](https://github.com/ggml-org/whisper.cpp/releases/tag/b5454)。
- GPU 引擎：官方目前没有 Windows Vulkan 发布包，因此使用 [fahrim/whisper-vulkan-win 的固定发布](https://github.com/fahrim/whisper-vulkan-win/releases/tag/engine-vulkan-b5454-static)，其公开构建流程从上游 whisper.cpp v1.9.5 编译。它是社区构建，发布包 SHA-256 固定在代码中。
- FFmpeg：优先复用已安装的版本；缺少时下载 [Gyan FFmpeg 8.1.2 essentials](https://www.gyan.dev/ffmpeg/builds/)，校验固定 SHA-256。
- 模型：[ggerganov/whisper.cpp 官方模型仓库](https://huggingface.co/ggerganov/whisper.cpp)，校验固定 SHA-256。网络不可达时设置页会报告失败，不会静默切换到未知镜像。

GPU 下载失败会保留可用 CPU 路径；模型或必要 CPU 依赖下载失败则等待用户重试。下载进度以实际读取字节数显示。

## 服务与排错

自动启动仅作用于默认 `http://127.0.0.1:8788` 或 `http://localhost:8788`；填写自定义地址时，扩展只连接该地址，不另行启动本机服务。助手空闲约 5 分钟且没有转写或配置任务时自动退出。

配置接口为 `POST /setup`（开始异步准备）、`GET /setup`（进度与机器信息）、`GET /health`（依赖与运行时状态）。视频转写仍使用原来的 `/jobs` 接口。浏览器网页来源不能直接触发配置或转写；扩展与本机命令行客户端可以调用。

启动日志：`%LOCALAPPDATA%\BOC\logs\whisper.log`。设置页提示“未安装本地启动器”时先重新注册；端口被旧助手占用时先停止旧进程，再点击自动配置。

目前自动注册和完整二进制安装针对 Windows x64 Chrome / Edge。macOS / Linux 可手动安装 whisper-cli 与 FFmpeg、启动 Python 助手，然后由助手自动下载模型；这些平台的 GPU 运行时自动选择尚未实现。Firefox 的助手连接与打包配置保留，但尚无自动注册安装流程。
