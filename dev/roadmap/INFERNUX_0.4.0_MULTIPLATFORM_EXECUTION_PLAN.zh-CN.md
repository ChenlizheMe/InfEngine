# Infernux 0.4.0 多平台构建执行计划

> 文档状态：立即执行的小计划
> 适用阶段：Infernux 0.4.0
> 上游前置：0.3.7 InxPackage/MCP Gate 已完成
> 后续阶段：0.4.2 开始 Torch、ModelRunner 与训练→部署闭环
> Release Gate：同一个普通 Python 项目能在 Windows/Linux Editor 中编辑与运行，并构建 Windows、Linux、Android、Web 四种可玩 Player

## 1. 一句话目标

把 Infernux 从“Windows 上能够制作和构建游戏”推进为“同一份项目内容、GUID 资产和普通 Python gameplay 能在 Windows/Linux 开发，并导出 Windows/Linux/Android/Web 四端可玩游戏”的真实多平台引擎。

## 2. 为什么 0.4.0 只做多平台

0.4.0 是后续 AI 能力的运行基础，不承担 Torch、模型推理或批量环境。先完成四端构建有三个直接目的：

1. 迫使 Player host、Cook、BuildProfile、资源、Python 运行时和平台生命周期形成明确边界；
2. 证明 Android/Web exporter 可以作为独立 InxPackage 工作，而不是把 SDK、模板和平台分支重新塞回核心；
3. 为 0.4.2 的 Torch/ModelRunner 提供已经可运行、可打包、可诊断的平台载体，避免推理系统反向决定 Player 能否成立。

版本号只表达递进关系，不约束发布时间。0.4.0 未通过本计划的 Release Gate 前，不以部分平台完成、原生库能编译或演示视频代替“多平台构建完成”。

## 3. 已知事实基线

以下是计划制定时从仓库和本机环境确认的事实：

- `CMakePresets.json` 的有效开发/测试预设以 Windows + MSVC 为主，另有未形成 CI 证据的 macOS 预设，没有 Linux、Android 或 Emscripten 正式预设；
- 根 `CMakeLists.txt` 已达到 1677 行，同时承担 target/source 定义、第三方依赖、平台分支、56 个 `add_test` 注册、源码树同步、Player runtime 预构建、wheel 安装、Hub、格式化和文档任务；继续直接叠加 Linux/Android/Web 分支会让平台移植和构建系统重构互相阻塞；
- 040 分支已拆分顶层 CMake，并以 `PythonWheel` install component 组装 `out/stage`；Release wheel 的唯一组装顺序固定为“原生模块/PlayerHost → 重新生成官方插件 InxPackage → 组装并校验 host wheel”。官方 `.inxpkg` 是被全局忽略的派生产物，不再由 Git 跟踪；所有 host wheel 都只携带默认 MCP 包，Windows/Linux/Android/Web 构建能力均由官方目录按需下载，避免平台构建包的独立更新节奏被核心 wheel 固化；
- 仓库已有 `out/build/<preset>`、`out/package`、`dist/releases/<version>` 的文字约定，但实际工具仍会创建顶层 `build/`，`out/` 根目录也混有测试项目、截图、日志、临时包和构建树；需要把生成物按 build/stage/test/package/diagnostics 分区；
- 当前 Preset 没有 hidden base、Linux、package 或 workflow preset，通用的 `release`/`debug` 名称实际绑定 Windows/Visual Studio；根 CMake 要求 3.22，Preset metadata 却写 3.20；当前主 CI 只有一个 Windows headless job；
- README 中对外平台状态仍是 Windows-only，`pyproject.toml` 却同时声明 Windows 和 macOS classifier；公开文档、包 metadata、Preset 和 CI 的平台口径必须由同一 support matrix 驱动；
- `CMakeLists.txt` 只在 `WIN32` 下创建 `InfernuxPlayerHost`；
- `python/Infernux/engine/game_builder.py` 明确拒绝非 Windows PlayerHost 打包；
- Player Cook、manifest、包审计、预构建 runtime 和 Windows PlayerHost 已经存在，可作为抽取统一 Build Graph 的基础；
- SDL3 已进入输入、窗口和音频输出路径，Jolt、Assimp、Vulkan、glslang、pybind11 等仍需逐个证明目标平台可构建；
- 当前 `InputManager` 的触控仍是 placeholder：只在 `FINGER_DOWN/UP` 上增减一个计数，`BeginFrame()` 又把计数清零；没有稳定触点 ID、位置、移动、phase、cancel、压力或接触面积。文本侧只累计 `SDL_EVENT_TEXT_INPUT`，没有把 IME composition/preedit 作为公开契约；这不足以支撑 Android 或移动网页；
- Headless 已有无渲染初始化、固定帧驱动和自动测试基础；Linux 已完成原生构建、GPU 测试、Player 真机 smoke，以及真实 L20 上的 GUI Editor 启动→按 GUID 恢复场景→原生打开/保存文件选择器→进入/退出 Play 自动闭环；真实桌面人工编辑体验仍需最终验收；
- InxPackage 已能承载官方插件，但尚不存在经过 Android/Web exporter 验证的通用平台构建扩展点；
- 本机 Windows 已安装 Vulkan SDK；WSL2 Ubuntu 22.04、WSLg、`/dev/dxg` 已存在；
- WSL2 已建立独立 `infernux` conda 环境并安装 Linux 原生编译依赖；仓库已有正式 Linux Clang Preset，并在 Ubuntu 24.04 真机上完成 clean configure、全量原生构建和测试；
- WSLg 当前的 Vulkan 只暴露 llvmpipe；因此它只能承担 Vulkan 正确性和 GUI 工作流调试，不作为 Linux GPU 性能证据；其它图形 API 的探测结果不计入 Infernux 证据；
- Emscripten 6.0.8 已安装；040 平衡球项目已生成带 CPython 3.13 runtime 的 HTML/JavaScript/WASM/data Web Player，并在无前台 Edge/WebGPU 中进入 ready 状态；
- Windows 已安装 JDK 17、Android command-line tools、Platform Tools、Android 36、Build Tools 36、CMake 3.30.5、NDK r29 与 Emulator；API 36 默认 AOSP x86_64 AVD 已完成冷启动；
- Web 最大技术不确定性是 CPython/WASM、静态原生模块和浏览器线程/异步语义，不是简单把 Vulkan shader 换成 WebGPU；
- Android 最大技术不确定性是 Player host、SDL Activity/JNI 生命周期、嵌入 Python 和构建/打包闭环，不是单独创建 Vulkan surface。
- Android 官方 CPython/cibuildwheel 工具链、Windows 与 WSL2 开发环境均已切换到 CPython 3.13；0.4.0 的源码构建、唯一 wheel ABI 与 HubInstaller 默认运行时统一为 Python 3.13；
- Hub 已建立按 Python 小版本隔离的 runtime catalog、安装门禁和项目绑定：当前发行只打包 `python313`，旧 wheel/旧项目需要时可显式安装同级 `python312`，两者不得混装。

### 3.1 2026-08-30 本机准备结果与失败账本

| 项目 | 当前证据 | 结论/下一步 |
|---|---|---|
| Windows 构建环境 | `infernux` conda 已切换到 Python 3.13；VS 2022、CMake、Ninja、clang-format、Vulkan SDK 可用 | 作为 Windows 3.13 基线 |
| Linux Python/C++ | WSL2 内 `infernux` 为 Python 3.13，CMake/Ninja/GCC/Clang、pybind11 和项目 Python requirements 已安装 | 作为 Linux 3.13 工具链基线 |
| Linux GUI | WSLg 可承担 llvmpipe 正确性调试；Ubuntu 24.04 真机通过 X11/Xvfb、NVIDIA Vulkan 驱动和双 L20 中的 GPU0 启动 Editor 与 Player；当前源码下的 Editor 自动 smoke 正确载入并保存由 Windows 复制来的 040 场景，进入 Play 5.06 秒后正常退出，进入/退出转换约 62/27 ms，FBX 动画探针、物理脚本和渲染初始化均工作。当前 `8a8d9157` 又重新生成约 114 MB 的 cp313 wheel，并在全新 `/tmp` Python 3.13 venv 中联网安装完整依赖；不设置 `PYTHONPATH`/`LD_LIBRARY_PATH`，Editor 直接从 wheel 内 `_Infernux`、Vulkan backend、SDL3、Jolt、Assimp 等 native payload 启动同一 040 项目，X11 `:97` 映射出标题正确的 1600×900 窗口，日志确认 `ENGINE_LOADED`、MCP requirements 自动恢复且无 fatal/validation/traceback。此前物理 X11 `:0` 验证过 NVIDIA Vulkan surface resize；本轮进程 maps 也确认加载 NVIDIA 580.173.02 图形库。测试结束 Editor、Xvfb 与临时 venv 均已关闭/删除 | Linux `_Infernux` 改用 Python Module 链接，不再产生 `DT_NEEDED libpython`；wheel 内 native 模块 RUNPATH 仅为 `$ORIGIN`，打包 verifier 会用 `readelf` 阻止回归。编辑器纯净安装、插件 Python 依赖恢复、GPU 启动以及 SDL→XDG/GTK 原生打开/保存对话框成立；最终真实桌面人工编辑体验仍待验收 |
| Linux 原生构建 | Ubuntu 24.04、Python 3.13、Clang/LLVM 18 下 `linux-clang-headless` 与静态 runtime 的 `linux-clang-release` 均完成 1113/1113 构建；显式连接 X11 `:0` 与 NVIDIA ICD 后 57/57 CTest 全过，并生成约 110 MB 的 cp313 Linux wheel | 版本化 `llvm-ar-18`/`llvm-ranlib-18` 与 Conda `pkg-config` 遮蔽问题已固化进 CMake、Preset 和安装脚本；无 `DISPLAY` 的 SSH 会让 Vulkan surface 测试失败，不能误判为驱动失败 |
| Linux Player 真机 | 干净迁移的同一 040 项目已由统一 CLI 生成约 80 MB 的 Release 与 Development Linux Player；Release 构建约 9.99 秒，Development Player 在 NVIDIA Vulkan + Khronos Validation + 隔离 Xvfb 下查询 `Start/PlayerBall`，注入 W 后 z 从 -1.0 移至 7.2204（增量 8.2204），6.24 秒内正常关闭，fatal=0 | 已证明干净项目资产目录发布、PlayerHost 自动发现、静态 runtime、GPU 启动和真实输入→物理链；仍需人工视觉验收、发行包复现和 CI |
| MCP 子模块复现性 | 主仓库引用的 MCP 提交已经存在于子仓库远端主分支，托管平台递归 checkout 已成功 | 保持插件仓库提交先发布、主仓随后更新 submodule 指针的发布顺序 |
| Web | emsdk 6.0.8；040 平衡球项目当前 asset revision 为 `2a7cc4eac71780307e8ec89d`，使用 CPython 3.13、WebGPU、约 40.3 MB cooked data、13.75 MB release WASM 与独立 HTML/JS/WASM；本轮增量构建 98.91 秒并通过 package audit。浏览器加载后无需额外点击即可进入游戏并自动把键盘焦点交给 Canvas，窗口重新激活、页面恢复或可见性恢复后会重新建立焦点，文本输入桥激活时则不抢焦点。HTML 只负责 Canvas 容器、隐藏文本输入桥和加载页，不提供第二套 DOM 游戏 UI；Screen UI 已由共享 draw list 进入 WebGPU backbuffer。自动 smoke 已验证 W/↑ 分别驱动 Python/Jolt 后令 PlayerBall 水平位移约 7.03/6.93，且键盘、右键菜单拦截、指针、文本 composition、用户激活音频、Screen UI、天空和阴影连续运行后保持 ready，无 abort/device-lost/fatal。验收项目现以真实运行状态发布机器可判定标记：LineRenderer 轨迹长度 0.291、动态末端误差 0；GPU 粒子发射器驻留/播放且仿真时钟推进；外部 FBX `mixamo.com` take 时长 1.033333 秒且原生动画时间推进。该验收同时发现并修复 Web host 缺少 `_gpu_particle_state_was_preserved` 控制面接口的问题。Android 真机 Edge 146 又通过 ADB reverse + CDP 原生触控持续驱动左侧 action 区，PlayerBall 水平位移约 7.16，全程未采集截图、phase error=0；验收后已强停浏览器并关闭 ADB | 桌面键盘→Python→Jolt 以及 Web 的 LineRenderer/粒子/动画结构化 gameplay Gate 已成立；仍需当前 revision 的人工键盘/画面复验、Android Chromium 上三个新状态标记复验、真实双指与系统 IME/软键盘、Chrome 品牌矩阵、iOS Safari 和缓存更新 |
| Android SDK | JDK 17、SDK 36、Build Tools 36、Platform Tools、CMake 3.30.5、NDK r29、Emulator 和默认 AOSP x86_64 system image 已安装到 `E:\toolchains` | Android NDK clang 已产出 x86_64 Android object，工具链可调试 |
| Android Emulator | `Infernux_API_36` 已通过 WHPX 冷启动，ADB 为 device，API 36/x86_64，guest Vulkan=`ranchu`，host Vulkan device=`RTX 5070 Ti` | 模拟器可调试 Vulkan；Android baseline Vulkan profiles 当前未通过，不能替代 arm64 真机 Release Gate |
| Android arm64 真机 | 3200×1440 真机已完成自动安装、横屏、game appCategory、沉浸式 system bars、固定 DPI、触控/生命周期、Vulkan Surface 恢复和完整 Bloom+ACES 验收。删除未完成的瞬态图像显存 alias 后，人类确认随机 tile/复制粒子噪声消失，画面基本正常；粒子首发延迟亦已修正。开发 APK 继续使用可安装的 Gradle debug 外壳，但原生运行时改为带诊断符号的 RelWithDebInfo：同一 040 场景由约 43 FPS 提升到接近设备 120 Hz 上限，game-only 约 1.23–2.50 ms，bootstrap ready 约 1.26 秒。最新约 10 分钟热浸泡包含 24 次触控轨迹、两轮 Home→恢复和两次左右横屏切换，全程 PID 不变；系统帧率约 119.4→119.0 FPS，温度 35.1→39.8℃，PSS 起止约 347→348 MB且中途 GC 回落至 321 MB，未出现 Vulkan/Python/进程错误。此前 158 秒短测与 30 次触控亦通过。最新 arm64 APK 又在 19.78 秒自动验收中一次触控即到达 gameplay，并同时发布 LineRenderer 非零动态轨迹、GPU 粒子驻留/播放/时钟推进和 FBX 动画 take/原生时钟推进三项运行标记；Back、两轮 Home→恢复、PID、四次 3200×1440 Surface 创建均稳定，fatal=0、abandoned buffer=0。内容缓存会保留当前加一代回退，真机已由 10 代自动收敛为 2 代；测试结束应用均强停、ADB 均关闭 | 当前设备的核心、短时性能、10 分钟热稳定和 LineRenderer/粒子/动画 gameplay Gate 成立；剩余为第二台 arm64 设备交叉验证、正式 release AAB 安装路径及更长功耗数据。每轮继续强停应用、关闭 ADB 与 Gradle daemon，避免消耗电池 |

### 3.1 2026-08-31 四平台复验补充

- Windows：当前源码 Release 全量构建通过，57/57 CTest 通过；Python 全量门禁为 4756 passed、2 skipped、0 failed。无 MCP 的完整 Editor→Play→构建 Player→退出链路曾稳定复现退出访问违规，根因是 `cleanup()` 释放 GIL 后销毁 Python frame callback；绑定现强制在持有 GIL 时先断开 Python 回调，再释放 GIL执行原生线程/GPU 清理，完整 wheel 环境复验通过。同一 040 Development Player 在 31.87 秒内完成打包，无前台抢占的验收在 6.08 秒内完成启动、真实 W 输入和正常退出，`PlayerBall` 沿 z 轴移动 4.2077，fatal=0。Player 日志把 bootstrap ready 记录为 2.553 秒，并生成游戏 render target 复核图；正式 CMake wheel 路径已经验证 cp313 原生 payload。
- Release 组装链：连续两次 Windows cp313 Release wheel 构建确认插件生成 target 每次都在原生模块之后执行，五个官方 `.inxpkg` 的生成时间均刷新；早期 wheel 曾携带 MCP 与宿主平台包，该合同现已被“wheel 只携带 MCP、全部平台包按需下载”取代。Git 跟踪列表中已无 `.inxpkg`，生成文件由统一 ignore 规则排除；平台归档仍由构建图每次重建，作为 CI/Release 附件而不是 wheel 内容。
- Linux：本地全部已跟踪改动同步到 Ubuntu 24.04 + 双 L20 真机后，Clang Release 全量构建通过；在隔离 Xvfb、NVIDIA Vulkan ICD 下 57/57 CTest 通过。统一构建 CLI 用同一 040 项目在 18.90 秒内生成 Development Player；token-authenticated smoke 在 5.68 秒内完成真实 W 输入并正常退出，`PlayerBall` 沿 z 轴移动 5.7168，Khronos Validation 下 fatal/VUID=0。当前 cp313 Linux wheel 构建及原生 payload 校验通过，并在第二个全新 Python 3.13 venv 中完成安装、Editor 场景加载/保存、5 秒 Play 和退出；首次 fresh-venv 复验准确暴露 bundled MCP preload 早于其 pip requirements 恢复，现已把 requirements reconciliation 强制移到任何 bundled reload 之前，第二次全新 venv 复验中先恢复 `mcp/fastmcp`、随后 preload，日志无 preload error、Traceback、VUID 或引擎错误。所有 Player、Editor、MCP 与 Xvfb 测试进程均已关闭。
- Android：当前 arm64 Development APK 以 Python 3.13 + Vulkan 重新构建通过，大小约 63.5 MB、构建 72.85 秒；3200×1440 Android 16 真机自动安装、横屏、首次触控、LineRenderer、粒子、FBX 动画、Back 和三轮生命周期验收在 22.90 秒内通过，fatal=0、abandoned buffer=0。30 秒系统帧样本的 jank 为 1.05%、P95 CPU/GPU frame time 为 8/4 ms、PSS 约 328 MB、温度 31℃；该样本只证明 UI/合成器没有卡顿，不能冒充引擎 game FPS。验收后应用、ADB 与 Gradle 均已关闭。当前源码又使用独立验收 keystore 走通签名 Release AAB：首次冷构建 312.26 秒，缓存后重建 24.79 秒，产物 66.19 MB；manifest 明确记录 `artifact_kind=aab`、`signed=true`、Python 3.13、Vulkan、arm64-v8a，bundletool 1.18.0 已按 3200×1440 真机生成 47.66 MB 的设备 APK 集。AAB 审计确认 18 个 arm64 native library、零其它 ABI、零 Torch/Numba 路径；NumPy 自带的 `random/_examples` 也已从权威 runtime 裁剪规则移除并由测试锁定。设备安装提交仍被手机系统的“USB 安装”安全策略以 `INSTALL_FAILED_USER_RESTRICTED` 拒绝，因此 bundletool 真机运行 Gate 保持打开；没有绕过系统策略，旧 debug 测试包已卸载，应用/ADB/Gradle 均已关闭。
- Web：最新 asset revision 为 `4424474708ba74d18bd97564`，Release 增量构建 148.93 秒并通过 package audit。真实浏览器复验中页面自动 ready、Canvas 自动聚焦，浏览器级 W 输入到达 Python action map（`vertical=1.000`）；LineRenderer、GPU 粒子、FBX 动画、ScreenUI、天空、阴影、音频和物理均发布运行标记，粒子输出为 HDR（`tint=4.9538...`、`rgba16float`），项目 RenderStack 的五级 Bloom 与 tonemapping 生效，当前 revision 无 page/console fatal。固定 1280×720 游戏画面在 984×912 窗口中按 16:9 缩放并居中，不使用 DOM 游戏视觉层。
- WebGPU 固定帧证据已修正此前的采样污染：Host Player 原先绕过共享 Build Settings，在验收 CLI 下静默采用 1920×1080 全屏无边框；Web 则按项目 1280×720 渲染后，在 960×540 CSS Canvas 与 DPR=2 的浏览器截图中二次缩放为 1920×1080。两张文件尺寸相同但并非同一像素网格，因此旧的模糊度、Bloom 扩散和局部颜色差异不能用于调参。现在 Windows/Linux Host、Android 与 Web 统一从请求快照或严格项目文档读取同一套经 schema 校验的设置，缺文件、损坏、非法字段或非正分辨率均立即失败；确定性 Web 捕获强制 `viewport == CSS Canvas == render target == 1280×720` 且 DPR=1，任何浏览器重采样都会使 Gate 失败。
- 新的 Vulkan/WebGPU 原生像素 Gate 在固定 1/60 秒、第 120 帧、相同 1280×720、相同相机/动画/曝光下完成。关闭局部光与粒子后的隔离帧全画面 MAE 为 **0.001671**、变化像素 **0.784%**；恢复两个不投射阴影的点光源、GPU 粒子、五级 Bloom 与 ACES 的完整 040 场景后，全画面 MAE 为 **0.001745**、变化像素 **0.834%**，粒子区域 MAE **0.004970**、角色 **0.002090**、LineRenderer/球 **0.002525**、发光/Bloom **0.001848**，全部通过当前固定帧阈值。四态粒子 A/B 仍独立证明粒子写入 RGBA16F scene target 并贡献 Bloom halo。PBR、蒙皮角色、方向阴影、透明、LineRenderer、GPU 粒子、Bloom 与 ACES 的 040 parity gate 据此关闭；capability validator 现拒绝任何 040 必需能力重新变为 `unsupported` 或 `open`。按同一严格 Build Settings 重建的 Web Development 产物 revision `85e40730ed39be055d0ba5b3` 通过 package audit，导出的 `open_parity_gates` 为空；这不包含仍明确标记为非 040 `unsupported` 的局部光阴影。
- `infernux-webgpu-capabilities.json` 继续公开真实边界：040 使用的点光照已登记为 `different-but-validated`，但 spot light 尚无独立可见 Gate，点/聚光阴影、custom surface、Toon、alpha clip 和完整 RenderStack effect catalog 仍为非 040 的 `unsupported`，不会因为当前场景像素接近而虚假宣称支持。Web 仍使用 backend-native WGSL PBR/后处理实现；后续 shader/material contract 共享化属于消除第二套语义实现的架构工作，而不是当前 040 固定场景视觉正确性的阻断项。
- 构建设置主链收敛同时暴露并修复了真实品牌资源重复：040 项目同一熔炉图用于应用图标和 Splash 时，旧 Host 会把相同字节分别装入通用 Runtime 图标、`Branding/icon.png` 与 `Splash/logo.png`。现在配置项目图标后不再携带通用运行时图标，相同 Splash 直接复用 `Branding/icon.png`；配置的 Splash 缺失或类型非法会明确失败，不再警告后继续生成不完整 Player。更新后的 Windows Development Player 通过 duplicate-payload package audit。
- Android Splash 已按当前 Player 合同完成真机修正：构建产物显式记录唯一 cooked `<Game>_Data` 根目录与 `logo`/`contain`/`cover` 布局，Activity 不再递归猜测资产根，Logo 以视口 45% 的安全区域保持宽高比居中显示。人工验收确认尺寸与居中正确后又发现渐变卡顿；真实调用链表明 Splash 在进入 Play 前被 `InxView` 的 Editor 10 FPS idle cadence 限制。`SplashPlayer` 现在以 monotonic clock 驱动，并在动画存活期间每帧向原生窗口请求 full-speed frame，动画完成后立即退出该调度，不增加计时或平台 fallback。重建 APK 在 3200×1440 真机完成 Python 3.13、Vulkan、Screen UI、LineRenderer、粒子和动画启动，日志无 fatal/validation/Traceback；应用已停止以保护电池，渐变流畅度保留为本轮人工视觉确认项。

### 3.2 2026-09-04 Player 内容密封与插件路径收口

- “加密”在 0.4.0 中严格指二进制资产序列化、GUID 寻址与工程目录隐藏，不宣称密码学保密或 DRM。`Content.inxpkg` 继续作为唯一内容容器；桌面收尾不再把它解包为 `Assets/Library/Packages/ProjectSettings`，`BuildManifest.json` 与运行时资产目录一同密封进 `AssetCatalog.inxcat`，Player 通过 `PackageIndex.inxmanifest` 验证并读取，不另造第二套包格式。Windows、Web 和 Android 新产物中上述五类松散条目均为 0；Android APK 只携带 `AssetCatalog.inxcat`、`Content.inxpkg`、`PackageIndex.inxmanifest`、`Player.inxmanifest` 及平台运行时。
- 公共资源 API 采用 Editor/Player 双态但单一语义：Editor 中路径解析到项目文件及其 `.meta` GUID；Cook 冻结 path→GUID→artifact binding；Player 中相同逻辑路径经密封 catalog 反查内容，必要时才导出到产品私有的当前内容缓存以供 `exe/jar/wasm/pyd` 等需要真实文件系统路径的外部消费者使用。找不到 GUID、产物或包所有权时直接失败，不扫描明文工程树、不按文件名猜测，也不退回源码路径。
- `Packages` 与 `Assets` 同为可编辑资产根：插件脚本、材质、Shader、网页、文本和任意携带文件均生成显式 `.meta`，参加资源刷新、脚本候选发现和导出依赖图。用户手动更新 `Packages/<reference>` 后可在当前 Editor 会话刷新并重新注册组件，不要求重启。项目 `Cache/Plugins` 仍只是下载/安装事务缓存，不生成资产 GUID。
- 官方 Git 插件仓库固定为小写 `package/` 作为唯一入包根；README、CMake、Java/CPP/其它语言工程和独立的纯标准库 `package.py` 留在外层。脚本不导入 Infernux，只把 `package/` 生成确定性 `.inxpkg`。Editor 内本地作者则直接面对项目 `Packages/<name>`；缺少 `inx_package.json` 时导出器按目标包文件名生成默认 `name/reference`，存在时严格使用作者声明。
- 最新真实证据：Windows Development Player 的密封内容、UIText 精确文本与控制通道 smoke 通过；Web revision `d345e95196d73d40e49d7809` 的 WebGPU 首帧、天空、阴影、Screen UI、音频、键盘运动和插件文本路径通过；约 52.97 MB Android arm64 APK 在 Android 16 真机完成资源路径、真实触摸、Unity 风格 Touch API、Back、三轮恢复和 3200×1440 横屏 Vulkan 验收，fatal 与 abandoned buffer 均为 0。Linux 与冻结提交 CI 仍须在本轮改动提交后重新取证，不能复用旧提交结果关闭 Gate。

当前进度采用两层口径：四端“同项目可构建、可启动、核心 gameplay 可运行”的工程主链约完成 **94%**，0.4.0 功能性 Release Gate 约完成 **90%**。代码瘦身、全仓 fallback inventory 与进一步 owner 收敛不再作为 040 阶段，后续由独立计划重新确定范围和指标。当前剩余工作主要是本轮 Linux 密封包复验、真实 CI 与 provenance、第二设备和浏览器矩阵、真实多 DPI 硬件证据，以及冻结提交上的最终公共 API 与四端回归；本地单元测试或模拟器不能替代这些外部证据。

这张表只证明本机调试条件，不计入 0.4.0 Release Gate。Release Gate 仍要求引擎产物、同项目回归、CI 和发布证据全部通过。

## 4. 范围和明确非目标

### 4.1 0.4.0 必须交付

- Windows GUI Editor；
- Linux GUI Editor；
- Windows Headless Host；
- Linux Headless Host；
- Windows Player；
- Linux Player；
- Android APK 和 AAB；
- Web 的 HTML、JavaScript、WASM 和资源目录；
- Android/Web 两个官方平台 InxPackage；
- 同项目四端可玩回归；
- 本机 Windows + WSL2 + Android 模拟器 + Tier 1 浏览器调试闭环；
- Windows/Linux CI、Android emulator smoke 和 Web browser smoke；
- 平台能力报告、包审计、错误诊断和 evidence manifest。
- Hub 多 Python 版本目录、运行时 catalog、兼容性解析、按目标版本安装/创建项目，以及 3.13 默认源和非破坏式 3.12 项目识别；
- Android 与移动 Web 的多点触控、Pointer cancel/capture、最小 action mapping、软键盘/IME、safe area、旋转/前后台恢复完整输入闭环；
- 行为不变地拆分顶层 CMake，并让 source-owned targets、平台策略、依赖、测试、安装、打包和开发工具各自有明确归属；
- 建立 Windows/Linux 的正式 configure/build/test/install/workflow Preset，以及 Android/Web 官方插件自有的交叉编译 Preset/模板；
- 统一 `out/` 与 `dist/releases/` 产物布局；五个官方 `.inxpkg` 统一生成到 `out/build/<preset>/official-plugins`，编译与发布结果不回写 `resources/official_packages`。只有 MCP 会从构建目录复制到 `python/Infernux/resources/infernux.mcp.inxpkg` 供源码 Editor 与 wheel staging 使用；四个平台包只作为 Release 附件，必须按需下载。临时插件归档必须每次由 CMake 重建且永不纳入 Git；“构建 wheel”仍不得隐式等价于“安装 wheel”；
- 建立分层多平台 CI、发布产物 provenance，以及与真实支持状态同步的中英文 README、构建文档和 package metadata。
- 在其它多平台任务完成后完成 Windows Editor 的 Per-Monitor DPI 自适应；以 1920×1080、100% 缩放为视觉基准，在 2K/4K、150%/200%/250% 缩放和混合 DPI 多显示器上保持一致的布局密度、字体清晰度、交互坐标与面板可用性；
- 统一面向用户的 `import infernux as inx` 公共 API、类型存根、模板、示例、文档和四端脚本验收；
- 在全部功能 Gate 基本冻结后执行代码瘦身与 fallback 审计：增强唯一主路径、删除废弃/重复实现和冗余 debug，仅保留具有设备或平台证据的显式兼容分支。

### 4.2 0.4.0 不做

- Torch、ExecuTorch、ONNX Runtime、ModelRunner 或模型资产；
- Android/Web Editor；
- Android/Web 上的 Numba；
- Windows-headless 或 Linux-headless 游戏发布包；
- RGB observation、异步 GPU 回读、批量世界、训练或 replay；
- OpenGL、OpenGL ES、WebGL 或其它产品渲染后端；0.4.0 只支持 Vulkan 与 WebGPU；
- 任意 PyPI/native wheel 在 Android/Web 自动可用的承诺；
- 为未来平台预造没有真实消费者的通用 hook；
- macOS/iOS 发布承诺。
- 完整可视化 Input System/rebinding 编辑器，以及陀螺仪、加速度计、振动等高级移动设备能力；这些不得以占位 API 计作已支持。

## 5. 平台与产物矩阵

| 宿主/目标 | 开发形态 | 0.4.0 产物 | Python | 图形 | 自动验证 |
|---|---|---|---|---|---|
| Windows x64 | GUI Editor + Headless Host | Windows Hub 安装包/更新包、Windows wheel、Windows Player 目录 | CPython 3.13；Hub 可保留 3.12 槽位运行旧项目/旧引擎 | Vulkan | Windows CI + Hub clean-install/update + Player smoke |
| Linux x86_64 | GUI Editor + Headless Host | Linux Hub 安装包/更新包、Linux wheel、Linux Player 目录 | CPython 3.13；Hub 可保留 3.12 槽位运行旧项目/旧引擎 | Vulkan | Ubuntu CI + Hub clean-install/update + 无显示 Headless + GUI/Player smoke |
| Android | 无 Editor | APK + AAB | 嵌入式 CPython 3.13，模块白名单 | Vulkan | Windows Android Emulator + 后续 arm64 真机 |
| Web | 无 Editor | HTML/JS/WASM/资源目录 | CPython 3.13/Emscripten 路径；若失败须经决策记录选择受限 lowering | WebGPU | Chromium/Edge browser smoke |

Android 本地快速回归使用 `x86_64` 默认 AOSP 模拟器；发布与最终设备 Gate 使用 `arm64-v8a`。两者必须消费相同 gameplay、资源 manifest 和平台契约，不能维护两套 Player。

## 6. 目标架构与强制边界

040 采用 fire-forced 的单一产品路径：能力、资源、ABI、平台或原生绑定不满足契约时立即给出可定位错误，不得靠异常捕获、默认值、旧实现、近似版本或第二套渲染/输入/构建路径把失败伪装成成功。平台和硬件之间确有必要的显式能力分支不等同于 fallback；若必须增加失败后替代路径，需先写明不可消除的外部约束、可观察语义、性能/正确性代价与移除条件，并用对应平台测试覆盖。既有明确协议（例如官方目录离线状态、Git Release-first 后的源码快照、WebGPU 对压缩纹理的确定性 CPU 解码）必须作为可查询状态呈现，不能演化为层层尝试的静默兜底链。

### 6.1 核心只拥有平台无关构建协议

核心应提供并测试以下最小概念，名称可在实现评审时调整，但责任不可混淆：

- `BuildTargetId`：稳定目标 ID，如 `windows-x64`、`linux-x64`、`android-arm64`、`android-x64-emulator`、`web-wasm32`；
- `PlatformCapabilities`：线程、动态加载、文件系统、网络、音频、输入、图形、Numba、Python native module、持久化等能力；
- `BuildProfile`：开发/发布、调试符号、资源压缩、线程模式、目标 ABI、浏览器 header 要求等用户选择；
- `BuildRequest`：项目身份、目标、配置、输出目录、取消令牌和可观察进度；
- `BuildPlan`：可检查的 Cook、脚本分析、原生构建、资源打包、签名、审计和验证步骤；
- `BuildResult`：产物、manifest、日志、诊断、耗时、必要的内容指纹与可复现性数据；
- `PlatformExporter`：目标发现、doctor、plan、execute、audit 和 smoke 的当前唯一接口；
- `RuntimePayload`：Player host、CPython、原生模块、标准库、插件 native payload 与目标资源；
- `CapabilityReport`：构建前给用户和 Agent 的可读/机器可读差异报告。

构建 UI、CLI、Headless Host 和 MCP 必须调用同一个 build service。UI 只呈现进度与结果，不拥有第二套构建逻辑。

Player 产物与构建缓存必须严格分离。桌面 Player 在构建阶段形成唯一、已展开的私有 Runtime/Content 布局，启动时直接加载，不得把 CPython、引擎 Runtime 或完整项目内容按构建哈希再次展开到 AppData、系统临时目录或游戏目录下的 `PlayerCache`。运行期只允许产生确有设备依赖的 Vulkan 管线/着色器缓存、存档和用户下载内容，并由产品身份隔离且提供明确清理边界。Android 等受 APK 资产文件系统约束的平台若必须把 Python 可执行内容展开到应用私有目录，只保留当前安装版本所需的一份，不保留历史构建代次；长期目标仍是减少必须展开的内容面。Editor 的导入、插件与构建缓存统一位于项目 `Cache/`，删除该目录不得损坏 Assets、Packages 或最终 Player。

### 6.2 平台构建支持的按需插件边界

Windows、Linux、Android、Web 的 Player 构建能力最终分别属于 `infernux/platform-windows`、
`infernux/platform-linux`、`infernux/platform-android`、`infernux/platform-web` 四个官方 InxPackage。
Windows/Linux Editor 与 Headless 本身仍由对应 engine wheel 和原生发行制品提供，因为编辑器启动前不能依赖项目插件；
Player 的 exporter、doctor、模板和打包策略则统一使用插件 contract，不让“桌面平台”成为长期特例：

- Windows 与 Linux wheel 都只内嵌默认 `infernux/mcp`。任何 Player 构建目标均由独立平台 `.inxpkg` 提供；用户只用 `pip install` 安装 wheel 时，Build Settings 仍展示官方已知目标，但明确标记需要下载对应平台插件，未安装时不注册虚假的 exporter；
- wheel 中 `python/Infernux/resources` 下随包交付的 `.inxpkg` 组成该发行必须安装的 built-in package set。新项目首次初始化以及已有项目由该引擎版本首次启动时，bootstrap 直接从这个目录读取、按当前 InxPackage 结构解析并事务安装缺失项；目录不是远程缓存，也不是仅供发现的目录，放入其中就等于发行方声明“该 host wheel 必装”。安装记录进入项目 package lock，重复启动按规范化 reference 与发行 version 幂等跳过；结构不完整、GUID 冲突或引擎版本不匹配时明确失败，不能计算一份额外整包哈希，也不能静默从网络替换；
- Windows、Linux、Android、Web、跨主机桌面目标和未来平台均不进入 host wheel 的必装 resources 集合，由用户从官方注册表按需 Download/Import。下载归档进入当前项目 `Cache/Plugins`，不向系统盘散落隐式副本；

- 包内拥有 host 源码/模板、toolchain manifest、exporter、doctor、测试和目标资源；核心只拥有 §6.1 的协议、Cook、公共 Player 契约、构建 UI/CLI 和注册表；
- 每个平台 wheel 除官方目录 bootstrap 外只内嵌 MCP，不携带任何平台 exporter、浏览器模板或 Gradle 工程。源码构建可以在临时输出面生成全部官方归档，但 wheel staging 必须只复制 MCP；
- 核心另维护只读的 `PlatformSupportCatalog` 视图：构建面板可以展示“未安装”的官方目标及其能力说明，但不得把目录项注册为 `BuildTarget`/exporter；未安装目标显示“当前不支持，需要安装平台插件”，并提供跳转插件窗口、定位对应 reference 的入口。只有下载、事务安装和 lifecycle 注册全部成功后，它才成为可构建目标；CLI/MCP 对未安装目标返回包含所需插件 reference 的结构化 `platform_plugin_required` 诊断；
- 官方目录保存经过 Infernux 项目实际验证的版本、engine contract、URL、依赖和平台/ABI metadata。目录不可用时降级为离线状态，不阻断 Editor；本地 `.inxpkg` 导入仍可工作；
- 插件窗口增加明确的“官方注册表”来源/筛选项，并对每个条目显示官方兼容性验证状态、类别（Platform、Agent、Rendering 等）、兼容引擎范围、目标 host/ABI、下载/缓存/安装状态和来源版本；该状态只说明 Infernux 项目验证过对应版本，不是分发授权或安装门禁。官方条目、GitHub URL、其它托管源和 PyPI 搜索结果在视觉与数据来源上严格区分。Android、Web、Linux 以及未来的鸿蒙、PlayStation、Xbox 等平台都只通过该目录发现，不为每个未来平台修改核心包或硬编码菜单；
- 下载的 InxPackage 进入当前项目明确可见、可整体删除的 `Cache/Plugins/reference/version`，不写入系统临时目录或 AppData，也不计算整包 SHA-256。用户可在插件窗口对任一版本执行 Download、Re-download 和 Import：Download 只预载到项目 Cache，Re-download 以临时文件原子替换同版本缓存，Import 才把该版本写入项目 lock 并激活；项目只保存 reference/version/cache location，不把缓存当作资产；
- SDK、NDK、Gradle distribution、Emscripten、目标 CPython、Linux sysroot 等大型工具链不进入核心 wheel，也不进入 InxPackage。插件只声明 toolchain manifest，由 Hub/统一下载服务按需放入全局 toolchain cache，多项目复用；
- 远程下载必须进入统一可取消进度框，从解析目录、下载、断点续传、校验、解包、事务安装直到 exporter 注册全部可见；不设置任意硬体积上限，失败不得留下半注册目标或污染项目；
- 插件缺席时 exporter registry 中不存在目标；安装后出现；禁用/卸载后清除注册、菜单和后台任务。项目 package cache 只由该项目持有，工具链仍由 Hub 全局管理并提供显式清理；卸载插件不自动删除下载归档，用户可在项目缓存管理中清理；
- 插件升级不要求重新发布核心 wheel，但必须声明引擎版本区间、build contract 版本并通过 contract suite；插件能够生成自包含目标 runtime 的最终 Player，终端运行时不依赖 Editor 插件管理器存在；
- 所有安装入口在下载或修改项目前先解析当前 `ENGINE_VERSION`。目录/Git Release 可同时列出多个插件版本，resolver 只匹配 `package/inx_package.json` 中 `engine` 覆盖当前引擎版本的 release，并默认选择最高稳定 SemVer；无兼容版本时明确显示每个 release 的不兼容原因并禁止安装，不能退回仓库 HEAD 绕过门禁；
- GitHub 仓库 URL 的解析顺序固定为 Release-first：先读取非 draft 的 releases，优先消费 release 附带的机器可读 manifest 与 `.inxpkg` 附件，匹配 tag、插件 version、reference、engine spec 和附件名；下载后直接读取 InxPackage 自身 manifest，不额外计算整包 SHA-256。仓库根本没有任何符合 Infernux 发布协议的 release 附件时，明确进入“源码快照”来源分支，下载精确 commit 的源码快照并从中生成临时 InxPackage；该来源同样检查 `engine`，并在 UI/lock 中记录 commit SHA 与 `source snapshot`，不能伪装成正式 release，也不能在 release 解析失败后偷偷改抓源码；
- 官方插件 Git template 增加固定 Release CI：tag `vX.Y.Z` 必须与 `package/inx_package.json` 的 version 一致，`engine` spec 必须可解析；CI 通过仓库根部独立 `package.py` 生成确定性的 `.inxpkg` 和 release manifest，运行 package/contract tests 后上传 GitHub Release。模板不得依赖已安装的 Infernux，也不得让用户手写 release metadata；其它托管平台通过相同 release-manifest provider 接口接入；
- 执行顺序不反转：先用现有 Android/Web 插件和临时核心 Desktop exporter 完成 Linux、Android、Web 的真实兼容 Gate；四端运行与构建稳定后，再升级官方目录/Hub cache，并把 Windows 与 Linux exporter 一并从核心 `DesktopPlatformExporter` 抽成正式平台插件。插件化不能成为推迟渲染、输入、生命周期、安装或性能问题的理由。

当前交付状态：主仓已经有从真实官方插件构建产物生成 Release 资产的唯一脚本和 CMake target，依赖顺序固定为原生 `_Infernux` → 官方 InxPackage → Release assets。一次真实 `windows-msvc-dev` 构建生成 Windows/Linux/Android/Web 四个 `.inxpkg` 及四个 scoped release manifest，共 8 个文件；manifest 直接记录包内 `reference/version/engine` 与附件名，不增加签名或 SHA-256。Release workflow 固定检出发布 tag、安装该 Release 的唯一 cp313 Windows wheel、重建当前 tag 的官方插件并把 8 个资产上传到同一 GitHub Release；缺 wheel、重复 wheel、包内 metadata 不一致或资产数量不等于 8 时直接失败。当前工作树还没有对应的远端 0.4.0 Release，因此只计入本地工作流合同与真实 CMake 证据，不声称远端 Actions 已通过。Windows、Android、Web 三个平台包已经完成当前 Windows host 上的项目级生命周期验收：安装前目标不存在，事务安装/preload 后出现并可解析，卸载后全部立即移除；Linux 的同构门禁只能在 Linux host 运行，不在 Windows 上伪造目标。插件面板还删除了对旧 `cached_reference_path(..., verify=False)` 表面的残余调用，直接使用当前不计算整包 SHA 的共享缓存契约；插件/Build Settings/Release 资产组合回归 105 项通过。

Ubuntu 24.04 真机随后补齐 Linux 同构门禁。首次运行准确发现远端 conda 环境仍是同一路径较早安装的 0.4.0 内容，缺少后来进入 wheel 的 `host_player_export.py`；当前 cp313 wheel 归档中该模块实际存在，因此直接强制重装当前 wheel、未增加导入回退。重跑后 `linux-x64` 安装前不存在，`infernux/platform-linux` preload 后注册且可解析，卸载后立即消失。临时项目与 Hub 测试缓存已删除，Editor、Player、Xvfb 和测试 Python 进程均未残留，机器未重启。另有真实原生 InxPackage 端到端门禁把官方目录、同仓 scoped GitHub Release manifest、包下载、Hub `reference/version` 共享缓存、项目 Import 和卸载连成一条链；项目卸载后共享缓存继续存在，未执行整包 SHA 校验。该链与已有分段合同共 3 项通过。

### 6.3 Python 与原生模块边界

- 0.4.0 当前开发链、官方 wheel、Windows/Linux Editor/Headless/Player、Android Player 和 Web CPython spike 统一以 Python 3.13 为 ABI 基线，不同时维护 0.4.0 的 3.12 与 3.13 wheel；
- Windows/Linux Player 可以携带声明支持的 CPython 扩展；
- Android/Web 必须在构建前解析 gameplay 的可达依赖图；
- Host-only、Editor-only、Numba 和无目标实现的 native dependency 必须在构建前产生可定位诊断；
- 不允许通过字符串扫描假装完成依赖分析；至少结合 import graph、插件 manifest/native payload 和最终 package audit；
- Android 的 CPython 与 `_Infernux` 采用目标 ABI 静态/受控动态装配；
- Web 首先验证官方 CPython `wasm32-emscripten` browser 构建和静态模块路径；只有可复现 spike 证明不可维护后，才能通过 decision record 启用 portable gameplay subset/lowering；
- 若启用 lowering，必须明确列出语义差异、拒绝规则、迁移诊断和原项目不被修改的保证。

#### 6.3.1 Hub 多 Python 版本管理

Hub 不再把 Python 当作单一全局状态，而是管理一组按小版本隔离的运行时。3.13 是 Hub 默认提供的运行时源，并且 0.4.0 唯一绑定 3.13；项目只是继承所选 Infernux 版本的绑定，不另有“默认 Python”。3.12 可继续安装，用于打开绑定 3.12 的旧项目或安装仍声明支持 3.12 的旧引擎版本；未来版本沿用同一模型，不再通过改一批硬编码路径扩展。

运行时目录保持现有约定并按版本并列，不增加额外嵌套层：

```text
C:\Users\Public\InfernuxHub\runtime\
  python312\
  python313\

<project>\.runtime\
  python312\
  python313\
```

强制契约如下：

- 运行时目录键由规范化的 Python `major.minor` 生成，例如 `3.12 -> python312`、`3.13 -> python313`；补丁版本、来源、hash、平台和架构写入各目录自己的 marker，不进入目录名；
- Hub 的运行时目录页列出所有受支持版本及 installed/missing/update-available/broken 状态，允许用户显式安装、修复或移除某个版本；3.13 排在首位并作为默认选择，不能隐藏其它已支持版本；
- “安装 Python 运行时”和“安装某个 Infernux 引擎版本”是两个独立动作。每个 Infernux 版本唯一声明一个目标 Python 小版本；安装引擎时从 wheel/package metadata 自动取得该版本，不让用户另选 Python。本地没有目标 Python 时安装按钮禁用并给出精确提示，用户必须先显式安装对应运行时；同一 Infernux 版本若发布了互相冲突的 Python ABI，Hub 将其视为无效发行而不是暴露多个选择；
- Hub 不因创建项目、安装引擎、打开项目或启动编辑器而静默下载 Python，不把 3.12 自动替换为 3.13，也不在目标缺失时回退到系统 Python、conda、PATH 中其它解释器或最接近的版本；
- 创建项目时用户只选择已经安装的 Infernux 版本，不出现独立 Python 版本选择器，也不在这里重复 Python 安装门禁。Hub 从该引擎版本唯一绑定的 Python ABI 自动创建运行时；正常情况下对应 Python 已在安装引擎时通过门禁。项目 manifest 仍持久化派生出的 `pythonVersion`（至少 `major.minor`）与引擎版本，供启动、修复和审计使用；若运行时后来被人工破坏或移除，创建/启动只报告完整性错误，不把它设计成第二套版本选择流程；
- 项目运行时复制到与目标版本匹配的 `.runtime/pythonXY`。打开项目时只使用 manifest 绑定的版本；缺失时阻止启动并引导用户先在 Hub 安装该版本，然后显式修复/重建项目运行时；
- 已有仅含 `.runtime/python312` 的项目识别为 3.12 项目，不自动改写。迁移到 3.13 必须是用户发起的事务：新建 `.runtime/python313`、安装兼容引擎与 requirements、完成 import/ABI 验证后再更新项目绑定；失败保留原 3.12 环境；
- 同一个 Hub 可以同时维护 3.12 和 3.13；每个 Infernux 版本、每个项目在任一时刻都只有一个生效的目标 Python。不得把两个版本的 site-packages、DLL/so、缓存或原生插件 payload 混入同一运行时；
- engine wheel、InxPackage requirements、Player runtime 和 native payload 都按目标 Python/平台/架构解析。缺少精确 ABI 的包在安装或构建前失败，不能等到 Editor/Player import 时才暴露；
- 运行时下载源使用版本化 catalog 描述 URL、hash、平台、架构和补丁版本。3.13 成为默认 catalog 项；3.12 的已知条目保留给旧项目，但不再作为 0.4.0 默认开发 ABI；
- Hub 启动后若只发现 3.12 而没有 3.13，显示一次非阻塞迁移提示并链接到运行时目录页；提示不自动安装、不强制关闭旧项目，也不把“存在 3.12”误报为“Python 未安装”；
- 删除某个 Hub 运行时前列出依赖它的已安装引擎和项目；仍被引用时默认禁止删除。项目私有副本不因删除 Hub 源运行时而被静默删除，但会失去修复/重建来源并明确告警。

Hub 内部必须用结构化 `PythonRuntimeId`/catalog/compatibility resolver 代替散落的 `python312`、`python3.12` 和 DLL 名字。平台文件名（如 `python313.dll`、`libpython3.13.so`）由所选运行时派生；只有旧项目探测代码可以显式引用 `python312`，不得把 3.12 兼容写回新的默认路径。

#### 6.3.2 Hub 的 Windows/Linux 原生发行合同

InfernuxHub 是同一套产品逻辑的两个原生发行制品，而不是一个只在 Windows 打包、在 Linux 源码运行的工具。Windows Hub 与 Linux Hub 必须由各自原生 runner 构建，携带各自平台的 Python runtime catalog、安装器行为和更新应用器，并且只能管理与本机 OS/architecture 匹配的 engine wheel/runtime：

- Release 至少同时产出明确命名的 `windows-x64` 与 `linux-x64` Hub 安装制品和独立更新包；禁止把 Windows `.exe`、Linux 可执行文件或两套私有 Python 混进所谓“通用 Hub”归档；
- Hub update manifest 必须带精确 host platform，文件名也必须包含平台；更新器只选择当前 host 的 archive/manifest，Release 缺少当前平台配对资产时明确报告该平台尚无更新，不能回退到另一平台、源码启动或旧式无平台清单；
- Windows 安装器遵循 Windows 应用目录、快捷方式、卸载和权限约定；Linux 安装制品遵循 Linux 桌面入口、图标、用户级数据目录和卸载约定。两者共享业务模型和界面，不复制安装版本、项目、插件与 Python runtime 管理逻辑；
- Windows Hub 只安装 Windows wheel，Linux Hub 只安装 Linux wheel；目标 release 虽存在但缺少本机 wheel 时，在版本列表中显示明确的不兼容原因并禁用安装，不从源码构建、不调用 Wine、不取另一 OS 的 wheel；
- 官网下载页必须同时给出独立、可见的 Windows 与 Linux 下载入口，并链接到对应 Release asset。不得靠 User-Agent 静默重定向或把 Linux 藏在高级下载中；可以把当前系统作为视觉推荐，但用户始终能直接选择另一平台；
- 官网的 Windows/Linux 入口分别注明 host OS、architecture、Python/runtime 由 Hub 管理的范围和当前支持状态；高级 wheel 下载器同样按平台分栏，只展示该平台真实发布的 wheel；
- CI 必须在 Windows runner 上完成 Windows Hub build/install/update/project-open smoke，在 Linux runner 上完成 Linux Hub build/install/update/project-open smoke；一个平台的成功不能替代另一个平台。Release workflow 只有在两套清单、归档和安装制品都存在且通过本机 smoke 后才可发布。

040 不实现二进制差分更新。Hub 的当前更新路径固定为“发现新版本 → 展示版本与变更 → 用户确认 → 下载当前 host 的完整 Hub 更新包 → 关闭后原子替换 → 重启验证”；这样每个版本只有一个可安装基线，不维护任意旧版本到新版本的补丁矩阵。失败发生在替换前则保留当前安装，替换阶段失败则由外部更新器恢复本次被替换的文件；这属于更新事务，不扩展成常驻双实现或旧协议兼容层。

Hub 自身及其在线内容需要统一、可运营的数据面：

- 官网发布轻量、匿名可读的 Hub release catalog，至少包含 stable/latest 版本、每个 host 的安装制品与完整更新包 URL、最低 Hub/OS 要求、发布日期和 release notes URL。Hub 以该 catalog 作为产品更新接口，不直接把 GitHub Releases API、登录态或较低的匿名 API rate limit 暴露为产品依赖；catalog 可以把具体文件指向 GitHub Release/CDN；
- 更新检查有 `up-to-date`、`update-available`、`unsupported-current-version`、`network-unavailable`、`catalog-invalid` 五种明确状态。只有成功读取并验证当前 catalog 后才能宣称“已是最新版”；未登录 GitHub、DNS/代理失败或官网不可达都不能被吞成最新版；
- stable/latest/已安装版本必须同时可见。Hub 更新只改变 Hub 自身，不隐式迁移项目或已安装 Infernux 版本；旧 Hub 若低于 catalog 声明的最低可直接更新版本，必须给出对应平台完整安装包和人工升级说明；
- Hub 通知由官网发布独立、版本化的 notification feed，支持生效时间、过期时间、严重级别、适用 Hub/engine 版本和中英文正文。已读状态只属于本机；feed 拉取失败显示离线状态，不继续展示过期缓存为“新通知”，也不把通知与 GitHub 登录绑定；
- 社区页必须消费 Infernux 社区的公开数据接口，展示真实热门/最新帖子、作者、回复数、更新时间与原帖链接；加载失败呈现明确错误和重试，不再放静态占位内容或伪造热度。外部内容经过字段白名单与文本/链接安全呈现，但不额外复制一套社区数据库；
- Hub/插件/平台包的上传和普通版本身份不依赖整包 SHA-256。版本、reference、host/ABI、release asset 名和内容结构是当前协议；只有 Python runtime、编译工具链等直接执行且由外部供应商分发的固定二进制输入，才按供应链边界保留发布方给出的摘要校验。
- Hub 建立唯一的语义主题令牌表，亮色/暗色各自为相同角色提供值：window、surface、raised surface、input、border、primary/secondary/muted text、accent、selection、danger、disabled、focus 等。普通 view、hover 动画、绘制型控件、Splash、安装器、更新器和弹窗不得硬编码业务颜色；它们只能读取当前主题令牌。主题切换必须刷新样式表、palette、自绘控件缓存和已经打开的窗口，不要求重启；两种模式都要做关键页面与弹窗的视觉/对比度回归。

### 6.4 Headless 与 Offscreen 分离

- Headless 是安装后的 Windows/Linux Host 能力，不是 Player 包型；
- 纯 Headless 不创建窗口、不初始化 GPU，能加载项目、资产、插件、MCP 和 gameplay；
- 固定 timestep、simulation clock 和 seed 是公共启动参数；
- Offscreen GPU 是独立 capability；0.4.0 只要求不破坏其未来入口，不交付正式 observation/AsyncReadback。

### 6.5 图形后端边界：只保留 Vulkan 与 WebGPU

- Windows、Linux、Android 只有 Vulkan 产品路径；不得为跨平台进度增加 OpenGL/OpenGL ES fallback；
- Web 只有 WebGPU 产品路径，不实现 WebGL fallback；不支持 WebGPU 的浏览器应由 doctor 和启动诊断明确拒绝；
- WebGPU 必须接入现有 Vulkan RHI/RenderGraph 所定义的资源、同步、pipeline、shader 和生命周期语义，复用其上层渲染系统；不得复制一套独立材质、光照、粒子或场景渲染器；
- WebGPU 适配层只处理浏览器 API、surface、异步 device、资源限制和 WGSL/目标着色器差异；这些差异必须进入 capability report；
- SDL 或系统库探测到 OpenGL 不构成支持证据，CI 和 smoke 也不得使用 OpenGL 路径兜底通过。

### 6.6 CMake 拆分与 target 所有权

拆 CMake 是 0.4.0 的第一个实现性改造，但必须先用 Windows characterization 固定现状。它不是按行数机械切文件，也不是顺手改 target 名、链接边界和产物格式；第一轮只改变职责归属和调用组织，不改变可观察行为。

目标结构采用“源码目录拥有 target，`cmake/` 拥有跨 target 策略”的两层模型：

```text
CMakeLists.txt                         # 薄入口：project、全局 policy、options、add_subdirectory
CMakePresets.json                     # 稳定入口，只 include 项目内 preset 文件
cmake/
  InfernuxOptions.cmake               # 产品 feature/options 与合法组合
  InfernuxToolchain.cmake             # 编译器、Python、Vulkan、平台前置检查
  InfernuxDependencies.cmake          # 第三方依赖策略，不直接定义产品 target
  InfernuxInstall.cmake               # install components 与 stage contract
  InfernuxPackaging.cmake             # wheel/runtime/Hub 的无副作用 package 入口
  InfernuxDeveloperTools.cmake        # format、docs 等维护目标
  platforms/
    Windows.cmake                     # Windows host/runtime policy
    Linux.cmake                       # Linux host/runtime policy
  presets/
    Base.json                         # hidden base、公共输出和诊断策略
    Windows.json                      # MSVC configure/build/test/package
    Linux.json                        # Clang/GCC configure/build/test/package
    Workflows.json                    # PR、release workflow
cpp/
  CMakeLists.txt
  infernux/<subsystem>/CMakeLists.txt  # Foundation/RenderCore/VulkanBackend 等 source-owned target
  tests/CMakeLists.txt                 # native test 注册与 label
external/CMakeLists.txt               # 第三方 option 与 target 适配
```

具体目录名允许在实现时按现有 subsystem 调整，但以下责任不可退回根文件：

- 根文件不得保留成片 source list、逐测试 executable、平台 DLL 拷贝、wheel/Hub 命令或文档生成命令；
- 每个产品 target 的 source、include、compile definition 和直接 link dependency 跟随该 target 所在源码目录；
- Windows/Linux Editor、Headless 和公共 host 的 ABI、RPATH/loader、符号及系统库差异放在核心平台模块；四种 Player 的 exporter、toolchain manifest、模板与打包逻辑在 Phase 8 后归各自官方 InxPackage，不复制进核心平台目录；
- 第三方库以 target-scoped option 和小型 adapter 管理，禁止继续通过全局 `BUILD_SHARED_LIBS`、`CMAKE_BUILD_TYPE` 强制改写扩散隐式状态；
- configure-time 生成步骤必须声明输入、输出和失败诊断；可以延迟到 build-time 的 shader/patch 生成不得阻塞所有无关 target 的 configure；
- native tests 以 label 分层（core、render、vulkan-device、headless、performance、platform），平台 CI 只按能力选择 label，不通过大段复制 target 定义来形成不同测试集；
- CMake `install()` 形成稳定 stage contract，至少区分 runtime、editor/python、developer files 和 symbols；wheel、Player、Hub 只能消费 stage/install manifest，不能猜测某个 build 目录或源码目录里的文件。

拆分完成的判据不是“根文件变短”，而是：Windows 的 target graph、输出内容和测试结果不变；Linux 可以通过同一 target ownership 增量接入；修改某个平台 packaging 不需要触碰无关 runtime/test 定义。

### 6.7 Preset、构建目录与产物边界

`CMakePresets.json` 是开发者和 CI 的唯一标准入口；`CMakeUserPresets.json` 只保存个人路径、SDK override 和本地并行度，并保持忽略。采用官方支持的 include + hidden base + inheritance，避免为“平台 × 配置 × 用途”复制整块 JSON。package/workflow preset 需要 Preset schema 6，因此 0.4.0 统一把根 CMake、Preset metadata、README 和 doctor 的最低版本提升到 CMake 3.25+；不能留下彼此冲突的最低版本声明。

正式命名表达 host、用途和配置，不再让 `release` 隐含 Windows：

- configure：`windows-msvc-dev`、`windows-msvc-release`、`windows-msvc-headless`、`linux-clang-dev`、`linux-clang-release`、`linux-clang-headless`；
- build/test：与 configure preset 同名，另用 test label 控制 GPU、headless、performance 等能力；
- workflow：`windows-pr`、`linux-pr`、`windows-release`、`linux-release`，依次执行 configure → build → test → install/package；
- 四个 Player 平台插件维护各自的 package/cross-build preset include/template；核心仓库仍维护构建 Windows/Linux engine、Editor、Headless 和公共 PlayerHost 的 host preset，并只校验 exporter contract 与输出 manifest；
- 保留旧 preset 名最多一个短迁移期，并在完成脚本、CI、README 切换后删除，不长期维护两套入口。

生成目录统一为：

```text
out/
  build/<preset>/                       # CMake binary tree，仅可丢弃
  stage/<target>/<config>/<component>/  # cmake --install 的组装输入
  test/<suite-or-target>/               # JUnit、截图、轨迹和 smoke 证据
  package/<product>/<target>/<config>/  # wheel/Player/Hub/APK/Web 临时组装
  diagnostics/<task-id>/                # 日志、崩溃、doctor 和临时调查产物
  cache/<tool>/                         # 可删的仓库局部缓存，不放 SDK/NDK/emsdk
dist/
  releases/<version>/                   # 只放已验证、待上传的最终产物与 manifest
```

约束如下：

- 不再生成顶层 `build/`，不把截图、临时 InxPackage、stash 备份或诊断文件散落在 `out/` 根；
- 编译默认不写 `python/Infernux/lib`、`python/Infernux/_runtime_packs` 或 `_runtime_modules`；Editor/dev import 通过 stage 路径和显式运行环境完成；
- `package-python` 只造并验证 wheel，`install-python-dev` 才显式修改当前 `infernux` conda 环境；CI 和 release 禁止依赖先前环境里已安装的 Infernux；
- `package-player`、`package-hub` 与平台 exporter 使用独立 staging，清理某个产品不能删除另一个产品或源码树的构建 metadata；
- SDK、NDK、emsdk、Vulkan SDK 和下载缓存保持在仓库外，由 doctor 读取；Preset 只引用环境变量或 user preset，禁止提交个人绝对路径；
- `scripts/build` 只保留跨 shell 的薄编排和 doctor 入口，核心 target graph 留在 CMake，产品 Build Graph 留在 Python build service，避免第三套构建逻辑；
- `packaging/` 继续表示 Infernux Hub 产品源码，不再被当作所有平台产物的泛用目录。

### 6.8 README、支持矩阵与发布口径

- README/README-zh、官网、安装文档、`pyproject.toml` classifier 和 Release notes 共用一份可审计 support matrix；
- 规划和开发期间可以写“experimental/under development”及本地构建步骤，但不能在 GUI/Player/CI/公开产物 Gate 前把 Linux、Android 或 Web 标成 supported；
- 每个平台文档至少包括：支持的 host/target/ABI、图形后端、Python 限制、安装/doctor、configure/build/test/package 命令、调试与日志、已知限制；
- README 的快速入口保持短：Windows/Linux Editor 源码构建、Hub 默认本机平台插件、Android/Web 按需插件和四端构建链接到分平台文档；不把完整 SDK 教程堆进首页；
- 官网下载页和 README 明确拆分 Windows Hub 与 Linux Hub：两条下载路径直达各自平台制品，安装步骤、支持状态和 wheel 高级下载不得混写；
- Release workflow 从 evidence manifest 生成平台表和 artifact 链接，人工文案只能补充说明，不能覆盖检测结果。

### 6.9 源码与仓库目录治理

目录整理服务于依赖方向和平台所有权，不做纯审美式全仓重命名。当前 `cpp/infernux/core`、`function`、`platform`、`tools` 已承载大量真实调用关系；第一轮 CMake 拆分只为现有文件建立 target owner，不移动源码。等 target graph 和 include 边界稳定后，再按下列责任渐进整理：

- `cpp/infernux/core`：不依赖 Editor、Python 和具体窗口系统的基础类型、生命周期与公共设施；
- `cpp/infernux/function`：现有引擎 subsystem 的过渡根；只有当模块依赖已被 target 证明时，才按 runtime/render/scene/resources 等实际边界迁移，不能一次性把 `function` 改名后仍保留原来的循环依赖；
- `cpp/infernux/platform`：跨平台抽象和 Windows/Linux host 实现；公共接口与 `windows/`、`linux/` 实现分开，Android/Web 实现仍归官方平台插件；
- `cpp/infernux/tools/launcher`：逐步收敛为明确的 `player_host`，不混入 Hub 或 exporter；`tools/pybinding` 只拥有 Python binding composition；
- `python/Infernux/engine/build`：最终只承载平台无关 Build Graph/service、contract、registry 和迁移期 facade；Windows/Linux/Android/Web exporter 最终全部从插件注册。旧 `game_builder.py` 在四端兼容 Gate 后按平台插件迁移计划拆除，不能把 Windows/Linux adapter 永久留作隐形核心能力；
- `packaging/`：只保留 Hub/installer 产品源码；`scripts/`：只保留开发与发布编排；`cmake/`：只保留 CMake policy/module，不堆放任意 Python 工具；
- 平台模板、Gradle 工程、Web shell、目标 runtime 和 SDK metadata 只进入对应 InxPackage，不作为松散核心资源安装；wheel 的 `python/Infernux/resources` 必须安装集合只有 MCP，其余平台能力全部从官方目录下载。

每次目录迁移独立提交，优先使用保留历史的 move，并同步 include/import、测试、文档和 ownership；禁止把目录搬迁、功能修复、ABI 修改和格式化混成一个提交。最终目录树应能让新贡献者仅凭位置判断“这是 runtime、host adapter、binding、构建协议、平台插件还是发行工具”。

### 6.10 MCP 是贯穿全阶段的效率保障线

MCP 已经是默认安装但可卸载的官方 InxPackage，也是 0.4.0 创建参考项目、操作 Editor、触发构建、收集诊断和执行自动 smoke 的主要 Agent 接口。它不单独占据一个等待全部完成的 Phase，而是贯穿 Phase 0–11：任何阶段只要真实使用证明 MCP 插件效果不好、操作缺失、权限提示不清、连接不稳定、响应过慢或结果无法验证，都可以立即暂停当前平台工作并修复 MCP 插件及其必要的核心 Host API。

允许随时修复的范围包括：

- Agent 无法通过现有 schema 完成当前人工可完成的场景、资产、组件、运行、构建或诊断操作；
- operation 粒度迫使 Agent 进行大量低效轮询、重复查询或超长调用链；
- 修改脚本、切换 Play、刷新插件或执行构建会无故踢掉当前 MCP session；
- 权限门禁只返回拒绝而不告诉 Agent 缺少什么 capability，以及用户应执行的具体授权方式；
- operation 的输入、输出、错误、进度、取消或幂等语义不足以支持跨平台自动化；
- Windows/Linux Editor、Headless 或平台 exporter 暴露出 Host API 缺口，导致插件只能越过稳定边界直接依赖内部实现；
- MCP 自身的启动耗时、schema 体积、调用延迟或失败恢复明显拖慢 0.4.0 调试效率。

边界与纪律：

- 修复必须来自 040 参考项目或平台流水线的可复现任务，进入统一缺口账本，并补 operation contract/integration test；
- 优先完善已有 schema operation 和 Host API，不恢复旧扁平工具，不保留旧名称兼容层；
- 构建相关 operation 只能调用 §6.1 的统一 build service，不能在 MCP 插件里复制 Cook、exporter 或平台打包逻辑；
- 引擎核心只提供事件、能力和稳定 Host API，MCP 协议、session、权限叙事和 Agent 适配继续留在插件；
- MCP 插件修改独立提交、独立测试并推送其仓库，主仓随后更新 submodule 指针、重建默认 MCP `.inxpkg` 并更新官方目录，不能只改本地插件工作区；
- 不借机无限扩张通用 Agent 功能；与当前多平台开发、调试或验证没有真实关系的新 operation 记入后续计划。

每个 Phase 的退出评审都增加一个问题：“本阶段有哪些人工操作仍不能由 MCP 稳定、可诊断地完成？”若答案影响下一阶段效率或自动验收，先修复再退出；若不影响，则记录明确 deferred reason。

### 6.11 Android 与移动 Web 输入是独立 Release Gate

“画面能启动”不等于“手机上可玩”。0.4.0 必须把桌面鼠标/键盘、原生 Android 触控和浏览器 Pointer Events 归一到同一公开输入语义，但不把 touch 粗暴伪装成鼠标。核心拥有平台无关 snapshot/event/action contract；Windows/Linux SDL adapter、Android SDL/JNI adapter 和 Web DOM/Emscripten adapter 负责采集与坐标转换。

#### 6.11.1 统一 Pointer/Touch 契约

每个活动指针至少暴露：

- 稳定且仅在该次接触生命周期内有效的 `pointer_id`；
- `device_type`（mouse/touch/pen/unknown）、`is_primary`；
- `phase`（began/moved/stationary/ended/canceled）；
- 当前 logical position、上一位置、delta、normalized position、timestamp；
- 平台可用时的 pressure、contact size；不可用时通过 capability 明确缺失，不伪造精度；
- 指针是否被 UI 消费、是否被 gameplay capture，以及 cancel/focus-loss 的来源。

活动触点状态跨帧保持，边沿事件按帧清理；`touch_count` 是当前活动触点数，不能再表示“本帧刚收到几个 down”。所有 down 最终必须有 up 或 canceled；暂停、失焦、页面隐藏、surface 重建和设备旋转会原子 cancel 遗留触点，防止虚拟摇杆卡死。事件队列可以保留同帧多个 move/coalesced sample，frame snapshot 则提供确定性的最终状态。

坐标必须同时区分：操作系统/window logical units、framebuffer pixels、Game viewport、UI canvas 和 normalized `[0,1]`。Android density、浏览器 CSS pixels、`devicePixelRatio`、letterbox、横竖屏和动态 resize 都通过同一 viewport transform，不能让脚本自行猜缩放。鼠标兼容事件与原始 touch/pointer 需要去重，防止一次点击触发两次；capture 后即使手指离开控件范围仍能收到 move/up/cancel。

#### 6.11.2 文本、软键盘与 IME

文本输入与按键输入分离，公开 API 至少包含：

- `start_text_input` / `stop_text_input`、焦点 owner 和 editable rect；
- committed UTF-8 text；
- IME composition/preedit、selection/cursor range、composition update/commit/cancel；
- input purpose/hint（text、password、email、URL、integer、decimal、phone、search）以及 multiline、autocorrect/capitalization 建议；
- backspace/delete/enter/tab/escape 或平台 submit/cancel 的明确语义；
- 软键盘显示状态和 keyboard/IME inset；无法可靠查询时报告 unknown，不以固定高度猜测。

中文拼音、候选词、emoji、组合字符、粘贴和多行输入必须以真正的 composition/commit 流程测试，不能只测试 ASCII `input_string`。Android 使用获得焦点的文本桥与 IME/insets 协作；Web 使用可聚焦的 DOM input/textarea 桥接 `composition*`、`beforeinput/input`，不能依赖 keydown 猜文本，也不能只依赖兼容性有限的 VirtualKeyboard API。软键盘出现后，当前输入框和提交按钮必须仍位于 visual viewport/安全区域内。

#### 6.11.3 Android 专属行为

- 正确处理多指 pointer ID 与可变化的 index、DOWN/POINTER_DOWN/MOVE/POINTER_UP/UP/CANCEL；
- Android Back 先遵循“关闭 IME → 关闭当前模态/UI → gameplay cancel/pause → 系统退出确认”的可配置顺序，不能直接杀 Player；
- 使用实时 WindowInsets 获取 system bars、display cutout、system gestures 和 IME，重要文本与触控目标不落在遮挡或系统手势保留区；不得硬编码状态栏、导航栏或键盘高度；
- 横竖屏、分屏、分辨率/DPI 改变、Activity pause/resume、surface destroy/recreate 后重建 viewport，并 cancel 旧输入状态；
- 外接键盘、鼠标和基础 gamepad 与 touch 可并存；设备热插拔不能清空其它设备的有效状态；
- 触控采样、UI hit test 和 gameplay snapshot 在高刷新设备上保持有界延迟，不因 Python 每个 move 单独跨语言调用而淹没主线程。

#### 6.11.4 移动 Web 专属行为

- Canvas 以 Pointer Events 为标准入口，保留 `pointerId/type/cancel/capture`；针对游戏 canvas 明确设置 `touch-action`，同时允许嵌入页面在非游戏区域保留浏览器滚动；
- 去除 touch 后浏览器生成的 compatibility mouse 重复事件；处理 `pointercancel`、`lostpointercapture`、`visibilitychange`、页面切后台和导航手势；
- 用 layout viewport、visual viewport、canvas CSS size、framebuffer size 和 `devicePixelRatio` 组成唯一坐标变换；软键盘、地址栏收缩、旋转和 pinch/zoom 策略变化都触发重算；
- 使用 `safe-area-inset-*`/平台等价信息生成 safe area；全屏、pointer lock、音频解锁、剪贴板和软键盘等需要 user activation 的动作必须从真实用户手势触发或显示可理解提示，不能静默失败；
- VirtualKeyboard API 只作增强路径；基础输入必须在没有该 API 时依靠 DOM focus、composition 与 visual viewport 正常工作；
- 自动化至少覆盖固定 Chromium 的 Pointer Events；最终移动 Web Gate 必须在 Android Chrome 和 iPhone/iPad Safari 的真实触屏、软键盘和 WebGPU 环境验收，桌面浏览器的手机尺寸模拟不能代替真机。

#### 6.11.5 Gameplay Action 最小层

0.4.0 不要求完整的可视化 Input System/rebinding 编辑器，但必须有一个最小 action mapping，使同一 gameplay 能把键鼠、gamepad、屏幕摇杆和触控按钮映射到 `Move/Look/Submit/Cancel/Pause` 等语义 action。UI touch controls 产生 action value，而不是伪造键盘事件；脚本仍可读取 raw pointer 处理拖拽、绘画或自定义手势。复杂手势识别、传感器、振动和完整重绑定 UI 可后续扩展，但未实现的能力必须在 capability report 中明确，不能以空 API 假装支持。

输入系统的最低验收任务是：单指点击/拖拽、双指同时控制移动与视角、触点离开/取消不粘住、中文和 emoji 文本输入、软键盘不遮挡输入框、旋转/切后台恢复、Android Back、移动网页滚动隔离，以及同一 action-driven 角色在桌面与手机上完成同一关卡。

自动化输入不能继续只有 synthetic key/mouse/text。测试与获授权的 MCP 输入注入必须支持多指 down/move/up/cancel、pointer ID、viewport 变化和 composition start/update/commit，并进入与物理设备完全相同的归一化队列；不得为了让测试通过直接改最终 `InputManager` 状态或绕过 UI hit test。真机测试仍用于覆盖 OS keyboard、手势导航和浏览器 user activation 等无法忠实合成的边界。

## 7. 本机开发与调试拓扑

### 7.1 桌面参考项目

在 Windows 桌面建立专用项目：

`C:\Users\陈立哲\Desktop\InfernuxMultiPlatform040`

项目必须是普通用户项目，不得引用仓库私有测试 helper。建议内容：

- 一个可移动角色或平衡球式物理对象；
- 静态网格、蒙皮动画、LineRenderer、粒子、材质和阴影；
- UI 按钮、单行/多行文本框和平台/能力信息；
- action-driven 角色控制：键鼠/gamepad 与屏幕双摇杆或摇杆+按钮驱动同一 `Move/Look/Submit/Cancel/Pause`；
- 单指点击/拖拽、双指并发、触点 cancel、软键盘、中文 IME/emoji、Android Back、旋转和屏幕安全区；
- 音频输出；
- 场景切换、保存数据和资源异步加载；
- 普通 Python gameplay，不依赖 Torch/Numba；
- 固定 seed 的结构化状态轨迹，用于四端比较；
- 明显显示 build ID、target 与资产目录 revision，防止误测旧包；资产身份由 GUID 与当前目录共同判定，不再额外生成一份整目录 SHA-256。

该项目在 Windows Editor 中维护，通过 Git 或显式同步进入 WSL2 的 Linux 文件系统；Linux 日常编译不得长期在 `/mnt/c` 或 `/mnt/e` 上进行，以避免跨文件系统 I/O 扭曲性能和文件监视行为。

### 7.2 Windows

- 将现有 `infernux` conda 环境升级到 Python 3.13，并重编 `_Infernux`、PlayerHost、wheel 和全部 native dependency，作为 Windows Python/C++ 测试入口；升级前的 3.12 测试结果只作为迁移对照，不算 0.4.0 最终 ABI 证据；
- Visual Studio 2022、CMake、Vulkan SDK 继续作为 Windows 基线；
- Windows Player 是统一 exporter contract 的第一个适配器和回归基准；
- 不先重写现有构建器；先用 characterization tests 固定其可观察行为，再逐段移入平台无关 Build Graph。

### 7.3 WSL2 Linux

- 使用现有 Ubuntu 22.04 WSL2 与 WSLg；
- 将 WSL2 `infernux` 环境升级到 Python 3.13，安装 GCC/Clang、CMake、Ninja、对应 Python 3.13 开发文件、Vulkan loader/tools、Mesa、SDL/音频调试工具和构建依赖；
- 在 WSL ext4 中维护 Linux 构建 checkout/worktree；
- 先完成纯 Headless 编译和测试，再启动 WSLg GUI Editor；
- WSLg 只承担本机开发反馈，不替代 Ubuntu CI 和真实 Linux 发行环境证据；
- Linux Player smoke 在 WSL2 和 CI 各运行一次，GUI smoke 记录 Vulkan device/driver。

### 7.4 Android

- Android SDK 和模拟器安装在 Windows 宿主，避免 WSL2 嵌套虚拟化；
- 使用 Microsoft Windows Hypervisor Platform；安装后用 `emulator -accel-check` 验证；
- 安装 JDK 17、Android command-line tools、platform-tools、稳定 platform/build-tools、固定 NDK、CMake、emulator 和 `x86_64` 默认 AOSP system image；基础 Player smoke 不依赖 Google Play/Google APIs，避免引入无关体积和服务变量；
- 创建一个固定名称的 Pixel 类 AVD，冷启动、Quick Boot、软件/硬件图形模式都能从命令行控制；
- 本地循环：export APK → `adb install -r` → 启动 Activity → 收集 logcat/tombstone → 执行输入 → 截图/状态探针 → 卸载；
- 首个模拟器 Gate 不等同于 Android 发布完成；最终仍需要至少一台 arm64 中端机和一台 arm64 高端机验证 Vulkan、触控、生命周期、音频、包体与冷启动；
- APK 用于快速安装测试，AAB 用于发布结构与 bundletool 审计。

### 7.5 Web

- 使用官方 `emsdk`，在 WSL2 中安装并冻结具体版本；
- 验证 `emcc`、`emcmake`、CMake/Ninja 和最小 C++/WASM 页面；
- 开发浏览器至少使用当前 Edge/Chromium，自动 smoke 使用固定 Playwright Chromium；
- 本地 server doctor 检查 MIME、缓存、压缩、COOP/COEP、`SharedArrayBuffer` 和 WebGPU；
- browser main loop 不允许阻塞；文件、资源下载、持久化和退出语义必须异步化；
- threaded 与 non-threaded 构建是否同时发布由 spike 数据决定，不能默认把 COOP/COEP 要求强加给所有部署；
- WebGPU 适配必须经过 adapter/device loss、surface resize、shader 编译和资源恢复测试；
- 首个 Web spike 必须先回答“普通 Python gameplay 如何运行”，再扩展完整渲染效果。

## 8. 执行阶段

### Phase 0：冻结基线和失败账本

交付：

- Windows Editor、Headless、Player 的现有成功证据；
- 当前键鼠、synthetic input、touch placeholder、文本输入和 viewport transform 的调用链与测试基线；
- 当前构建阶段时序、输入输出、manifest 和包审计 characterization tests；
- 当前 CMake target graph、Preset 展开结果、install/package 副作用和生成目录快照；
- 对 `python/Infernux/lib`、runtime pack、wheel 安装和顶层 `build/` 的写入者清单；
- 用 MCP 完成 040 参考项目初始创建/编辑/Play/构建探测，记录调用数量、耗时、断连、权限和缺失 operation 基线；
- 建立贯穿 Phase 0–11 的 MCP 缺口账本；
- Windows 专属路径清单；
- 第三方依赖平台矩阵；
- 040 桌面参考项目 v0；
- 风险/decision record 模板。

退出条件：能明确回答当前 Windows Player 从点击 Build 到启动 gameplay 的每一步由谁负责；能从干净 checkout 重现 Windows configure/build/test/package；失败账本覆盖 CMake、PlayerHost、Nuitka、Python runtime、资源、插件 native payload、源码树写入和审计。

### Phase 1：行为不变的 CMake、Preset 与目录治理

执行顺序：

1. 为现有 Windows target、产物文件集、CTest label、wheel/runtime pack 和 Hub 入口补 characterization；
2. 先把 tests、developer tools、Hub/docs、Python packaging 从根 CMake 移出，再按 Foundation/RenderCore/Renderer/Vulkan/Python/PlayerHost 的 target ownership 下沉；
3. 拆 `external/CMakeLists.txt` 的全局 option 污染，给第三方依赖建立平台 capability inventory；
4. 引入 `cmake --install` component 和 `out/stage`，让 wheel、Player runtime、Hub 从 stage 消费；
5. 把“造 wheel”和“安装到当前 conda”拆成两个显式动作，移除默认源码树 native/runtime 回写；
6. 把现有 Windows preset 改为 platform-explicit 名称，以 hidden base 组合配置；
7. 增加 Linux dev/headless/release configure/build/test/install/workflow preset；
8. 迁移脚本和 CI 后删除旧 preset alias；
9. 收敛 `out/` 子目录，清理工具只操作显式白名单；`dist/releases` 永远不由普通 clean 删除；
10. target graph 稳定后，再按 §6.9 小步整理 `platform`、PlayerHost、binding 和 Python build service 目录，不做全仓一次性改名。

迁移纪律：每一步只移动一类责任并立即在 `conda activate infernux` 环境下做 configure、对应 build 和测试对照；不得在同一提交中同时重命名产品 target、改变 ABI、重写 exporter 或修复无关渲染行为。

退出条件：根 CMake 只负责顶层编排；Windows characterization 全部一致；Windows/Linux Preset 可列出且路径不冲突；普通 configure/build 不修改 tracked/source package 目录；从 `out/stage` 可独立组装并审计 Windows wheel/runtime；一次 clean 不会跨产品或删除 release 产物；被迁移目录都有明确 owner 且没有兼容性 shim 长期残留。

### Phase 2：Hub 多 Python 版本、3.13 ABI 与本机 Doctor

交付：

- 用结构化 runtime catalog、版本目录解析和 compatibility resolver 替换 Hub 的单版本硬编码；
- 把 Hub 发行链拆为 Windows x64 与 Linux x64 两个原生产品：平台化 manifest/archive/installer 命名、host 精确更新选择、各自安装/卸载行为和 Release assets；
- 官网建立并测试独立 Windows/Linux Hub 下载入口，高级 wheel 下载也按 host 平台分开；
- 建立官网匿名 Hub release catalog，并让 Hub 的 latest 检查、平台制品选择、最低可升级版本与 release notes 只消费这一份产品接口；移除运行时对 GitHub 账号/API 的直接依赖；
- 更新器采用当前平台完整包替换，不实现二进制增量补丁；更新状态、用户确认、下载进度、替换、失败恢复和重启验证形成一条可测试事务；
- 通知页改为官网版本化 notification feed，社区页接入真实社区公开接口，并分别覆盖在线、无内容、离线、非法响应和过期数据状态；
- 移除 Hub、插件上传和普通 Release 资产身份上的整包 SHA-256 依赖，只保留 Python runtime/外部工具链供应链输入的必要摘要验证；
- 统一 Hub、安装器、Splash、更新器与全部自绘控件的语义主题令牌，清除 view 内亮/暗色硬编码；建立运行时主题切换和关键窗口的双主题视觉回归；
- 保留 `runtime/python312` 和项目 `.runtime/python312` 的旧项目识别，同时增加并默认使用同级 `runtime/python313`/`.runtime/python313`；
- Installs 页能分别显示、安装、修复和受引用保护地移除各 Python 版本；Hub 启动时在缺少默认 3.13 时给出非阻塞提示；
- 引擎安装严格校验目标 Python 已在本地安装；缺失时禁止安装并引导到对应 runtime 项，不触发隐式下载；
- 新建项目只列出已安装的 Infernux 版本，并自动保存该引擎派生出的 Python 版本；Python 存在性门禁只发生在在线/本地安装 Infernux 版本时；
- 已有 3.12 项目保持可识别、可启动；显式 3.12→3.13 迁移以新目录事务执行，验证失败不改变原项目；
- Windows、Linux、Android 和 Web 的 0.4.0 构建契约、wheel tag、native module、PlayerHost 链接、package audit 和 CI 默认 ABI 统一为 Python 3.13；
- Windows doctor；
- Linux/WSL doctor；
- Android SDK/NDK/emulator doctor；
- Web/emsdk/browser doctor；
- 版本锁文件与可重放安装说明；
- 不把个人绝对路径写进项目或插件产物。

退出条件：Hub 可同时安装并区分 3.12/3.13，0.4.0 引擎唯一绑定且项目自动继承 3.13；缺失目标 Python 时在线 Infernux 版本仍可见但安装禁用，本地 wheel 导入立即拒绝，两条路径都明确引导安装精确 Python 版本；新建项目不提供 Python 选择或第二套安装门禁；旧 3.12 项目未被改写；Windows 与 Linux 原生 Hub 都能从各自安装制品 clean-install、通过匿名官网 catalog 发现并执行完整包更新、打开项目，更新器不接受跨平台资产；无 GitHub 账号的新机器能区分“最新版”和“无法检查”；通知 feed 与社区真实帖子在在线/离线状态下都给出准确结果；普通 Hub/插件上传不要求整包 SHA-256；Hub 全部关键页面、自绘控件、安装/更新弹窗在亮暗主题下均只使用语义令牌且切换后立即正确刷新；官网两条下载路径能解析到同一 Release 的对应平台制品；Windows 与 WSL2 的 3.13 原生模块/测试基线成立；四组 doctor 在本机给出结构化通过/失败；WSLg 能打开 GPU GUI 测试；Android AVD 可由命令行启动并被 ADB 识别；浏览器能加载由 Emscripten 生成的最小 WASM 页面。

### Phase 3：统一 Build Graph 与 Windows 适配

交付：

- 冻结 §6.1 的最小接口；
- 把 Windows 专属 PlayerHost、SDK、图标和动态库逻辑移到 Windows exporter；
- Build UI/CLI/Headless/MCP 统一调用 build service；
- 进度事件覆盖 doctor、下载、Cook、编译、链接、打包、签名、安装和 smoke；
- MCP 的 build operation 返回与 UI 相同的结构化进度、取消、诊断和 `BuildResult`，长构建不依赖高频轮询，也不重启自身 session；
- 取消、失败恢复、临时目录清理和原子发布；
- Windows 产物与重构前行为对照。

退出条件：Windows Editor/Player/Headless 全量回归通过，Windows Player package audit 无退化；一个虚拟 exporter fixture 能独立注册、生成计划、报告进度和被卸载。

### Phase 4：Linux Host、Editor 与 Player

执行顺序：

1. Linux 原生核心、依赖和 pybind11 模块编译；
2. Linux 纯 Headless 加载项目、资产、插件和 MCP；
3. 文件系统、动态库、日志、崩溃、线程和时间语义修复；
4. WSLg Linux GUI Editor：SDL window、Vulkan、输入、音频、ImGui、文件对话框；
5. Play Mode、场景/资产保存、热重载；
6. Linux PlayerHost 与 Python runtime payload；
7. Linux exporter、包审计和 CI。

退出条件：040 项目在 Windows/Linux Editor 中编辑、保存、Play；Windows/Linux Headless 运行固定轨迹；Linux Player 独立启动并完成同一可玩任务。

当前真机证据：Ubuntu 24.04 + Python 3.13 + Clang/LLVM 18 已完成 headless/release 原生构建（1113/1113）、57/57 CTest 和 cp313 wheel；统一构建 CLI 可从没有 `Library` 缓存的干净 040 项目发布 GUID 资产目录、发现 preset PlayerHost，并生成约 80 MB 的 Linux Release/Development Player。仓库内 `linux_player_smoke.py` 以 token-authenticated Player control 而非终端日志判断就绪，在 Xvfb + NVIDIA Vulkan ICD + Khronos Validation 下查询 `Start/PlayerBall`、注入 W 键并观测 z 轴从 -1.0 移至 7.2204，6.24 秒内正常关闭且无 VUID/error/fatal；GUI Editor 打开并保存 040 场景，运行 gameplay 5.06 秒后正常退出 Play，进入/退出转换约 62/27 ms。为验证安装产物，另建全新隔离 Python 3.13 venv 并 pip 安装新 wheel；`_Infernux` 已改为 Python Module 链接，不再直接依赖 `libpython3.13.so.1.0`，wheel 内 native RUNPATH 仅保留 `$ORIGIN`，无需额外 library path 即可 import，并在物理 X11 `:0` + NVIDIA Vulkan 下从 clean wheel 启动 Editor，日志确认 `ENGINE_LOADED` 和 surface resize。插件管理器现会在 preload 前核对当前项目 Python 环境，并从当前或早期安装记录的插件控制文件恢复缺失 requirements；真实 fresh venv 已自动补装 MCP 依赖，MCP preload 无错误且 Editor/Play smoke 再次通过。2026-09-01 又按“原生→官方插件→wheel”重建约 114 MB 的 cp313 Linux wheel，在第二个不继承 system site-packages 的全新 venv 中安装并从 wheel 导入大小写两个入口；物理 1024×768 X11 桌面上，Editor 外框被确定性约束为 958×736@(+66,+32)，client/Vulkan surface 为 958×699，未再出现原 1600×900 窗口越界，且无 VUID、fatal 或 traceback。对应原生窗口尺寸 contract 在 Windows 与 Linux 均通过；随后在隔离 Xvfb + NVIDIA Vulkan 下执行更新后的 Linux Release CTest，**58/58** 全过（26.77 秒）。测试结束后 Editor、临时 venv 与 Xvfb 均已关闭/删除。Windows/Linux Headless 对同一项目以 1/60 秒固定步运行 180 帧，在 7 个采样点记录 `PlayerBall` 的位置、线速度和角速度，按 `1e-4` 容差比较无差异（实际最大浮点差约 `3.47e-17`）。所有验收结束后均未遗留 Editor、Headless、MCP 或 Xvfb 进程。Phase 4 尚未关闭，剩余项是 Linux 真实桌面人工编辑体验、四平台 CI 和最终冻结提交复验。

2026-09-03 更新：Linux wheel-only Editor 已在隔离 X11 + NVIDIA Vulkan + D-Bus 会话中真实触发 SDL 文件选择接口。XDG/GTK 打开和保存窗口均出现并分别返回精确预设路径，之后同一 Editor 正常进入/退出 Play 并关闭；扩展后的 `editor_project_smoke.py` 直接校验平台回调结果，不以 mock 代替系统窗口。文件对话框项据此关闭，Phase 4 现仅保留真实桌面人工编辑体验、CI 与最终冻结提交复验。

### Phase 5：Android 官方平台插件

执行顺序：

1. 插件注册 target、doctor 和空壳 exporter；
2. SDL3 Activity/JNI/NDK host 启动最小原生画面；
3. CMake 交叉编译核心与第三方库，先 `x86_64` 模拟器、后 `arm64-v8a`；
4. Vulkan surface、渲染、density-aware viewport 和实时 WindowInsets；
5. 多点 pointer 状态、move/cancel/capture、UI hit test 和最小 action mapping；
6. 文本焦点、中文/emoji IME composition、软键盘与 keyboard inset；
7. Android Back、横竖屏、暂停/恢复、窗口重建、后台/前台和低内存事件；
8. 外接键鼠/基础 gamepad 与 touch 并存；
9. 音频输出、assets/filesystem、日志与崩溃；
10. 嵌入 CPython 3.13、对应 ABI 的 `_Infernux` 和普通 gameplay；
11. APK、AAB、签名配置、package audit；
12. 模拟器自动安装/启动/input smoke；
13. arm64 真机矩阵。

退出条件：安装插件后 Android target 出现；040 项目输出 APK/AAB；模拟器和冻结真机用双指 action 控制完成同一可玩任务，中文/emoji IME、软键盘避让、Back、旋转、触点 cancel 和后台恢复通过；卸载插件后 target 与注册残留消失。

当前收敛状态：桌面 040 平衡球项目已同时输出 arm64 development APK 和 release AAB；AAB 使用 Vulkan + Python 3.13、关闭 debug symbols、启用资源压缩，package audit 通过。真机自动验收已证明横屏 Surface、单指移动、Back、两轮 Home→resume、PID 稳定和无致命/废弃 Surface 错误，但人类长时间验收曾暴露严重发热、低帧率、随机 tile 花屏与慢启动，因此 Phase 5 仍保持打开。当前候选包启用 mobile profile（0.5 内部分辨率、1024 阴影、1x MSAA）、FIFO、2 frames-in-flight，并按人工测量要求移除了软件 FPS cap；场景内提供 0.5 秒窗口的实时 FPS/平均帧时文本，不能再用受限帧率或不可靠的 SurfaceFlinger 厂商日志代替真实性能数据。卸载包和应用数据后自动安装的基线中，logcat 的 Activity→Python ready 为 0.59 秒、→engine ready 为 4.83 秒、→gameplay `start()` 为 5.40 秒，约 895 个 Python 文件没有成为这台 UFS 设备上的主瓶颈。RenderGraph 跨帧状态继承仍不能修复噪声；根因最终收敛到 transient image 显存 alias 缺少可靠 hand-off，同一 RenderGraph 的图像改为独立分配后，完整 Bloom+ACES 包经人工复验已“基本无异常”，此前的随机 tile/全屏噪声在当前设备上不再复现。该修复已通过 Android/native contract、Windows Vulkan 设备测试和最新 Windows Release 57/57 CTest；新增约 158 秒持续运行与 30 次触控轨迹注入，系统帧率约 119.9→118.4 FPS、温度 32.5→34.5℃、PSS 338→346 MB，未见 Vulkan/Python/进程错误，说明短时性能与内存趋势正常，但仍需 10–20 分钟热浸泡和第二档 arm64 真机防止过早关闭视觉 Gate。粒子延迟的调用链已确认是场景 `prewarm=true` 将完整 5 秒循环拆成约 300 个固定 GPU 步，而原生安全调度每次提交只排空一步；040 验收场景已关闭长预演并在 t=0 发射 12 个粒子，人工复验确认粒子仅在最初短暂等待后正常激活，长期 GPU Prewarm 仍作为独立调度缺口保留。Screen UI 的 CanvasScaler 已改为“参考分辨率只决定 scale，锚点使用当前屏幕除以 scale 后的实时逻辑画布”，修复 3200×1440 横屏中顶部 HUD 被负偏移裁掉的问题；最新 arm64 APK 在 24.8 秒内构建、自动覆盖安装，系统确认 3200×1440 横屏全屏及导航隐藏，Activity 冷启动 0.53 秒、约 4.4 秒进入引擎/场景，现等待人工确认 FPS 文本位置与稳定帧率。后续还需记录未限帧 HUD 的稳定 FPS、持续温度/功耗、长时 Adreno 呈现同步及第二档设备结果。真机安全锁屏时验收继续 fail-closed，自动脚本在无需人工观察的成功或失败路径都必须关闭应用。

2026-08-31 追加真机门禁：Android 同一 arm64 APK 在 16.39 秒内再次通过三轮 Home/resume、真实触控、3200×1440 横屏与 LineRenderer/粒子/动画/Screen UI 必选标记，fatal=0、abandoned buffer=0。约 45 秒未限帧采样中，系统 gfxinfo 记录 393 帧、jank 1.02%、P50/P95=5 ms、GPU 1–4 ms，PSS 约 316 MB，进程 CPU 约 61.5% 单核，电池 33.8→34.4℃、thermal status=0；数据进一步支持“持续发热主要来自无限制主循环/功耗策略而非当前 GPU 帧超时”的判断。按约束仍不偷偷加入 FPS cap，后续应提供可配置 frame pacing/功耗模式并保留 uncapped profile 作为测量模式；验收后应用与 ADB 已关闭。

Linux 全新 cp313 wheel 安装门禁首次暴露项目插件与 wheel 内嵌默认插件同 reference、不同内容时会被强制重装检查阻断 Editor。现已改为：resources 根目录中的 `.inxpkg` 对缺失 reference 仍是必须本地安装，但已有项目版本（可能更新、来自 Git/注册表或其它受支持来源）应保留。新增回归验证项目 payload 与 source record 均不被内嵌旧包覆盖。本地插件测试 64/64 通过；L20 真机重新构建 114,497,539-byte wheel，并在全新 Python 3.13 venv 中安装/import，从 Xvfb 1600×900 + NVIDIA Vulkan 启动同一项目，保存场景、进入 Play 2.99 秒、退出恢复全部通过，进入/退出总耗时约 82.20/44.05 ms。测试结束 Editor、Xvfb、临时 venv 与远端源码改动均已清理，Linux 未重启。

### Phase 6：Web 官方平台插件

执行顺序：

1. Emscripten 编译第三方依赖的 compile inventory；
2. 浏览器 host/main loop 与最小 WebGPU surface；
3. 把 Screen UI 的布局与绘制命令积累拆成平台无关 draw list，由 Vulkan 和 WebGPU 后端消费同一份顶点、索引、字体图集、纹理与裁剪语义；Web 正式画面必须进入 WebGPU backbuffer，DOM 只允许承担隐藏 IME/软键盘、粘贴和可选无障碍桥，禁止以 CSS/DOM 视觉副本冒充引擎 UI；
4. Pointer Events、multi-pointer/cancel/capture、compatibility mouse 去重和 canvas `touch-action`；
5. CSS/device pixel viewport transform、VisualViewport、safe area、地址栏/旋转/软键盘 resize；
6. DOM 文本桥、中文/emoji composition、输入类型与焦点/user activation；
7. 最小 action mapping、移动 UI hit test、页面隐藏/恢复和 WebAudio；
8. 资源下载、MEMFS/IDBFS、缓存与版本失效；
9. CPython 3.13 `wasm32-emscripten` + 静态 `_Infernux` spike；
10. import graph、模块白名单和目标诊断；
11. HTML/JS/WASM/资源 exporter；
12. server doctor、PWA/缓存边界和桌面/移动浏览器 smoke；
13. 线程模式、SIMD、COOP/COEP 与包体/启动预算；
14. 建立 Vulkan→WebGPU render-feature inventory，逐项核对 RenderGraph pass、attachment、binding、draw/dispatch、混合/深度状态、纹理格式和最终合成；任何缺口必须标为未实现，不得以组件存在或 CPU/GPU 仿真时钟推进代替可见输出；
15. 补齐 040 项目要求的 WebGPU 可见链：PBR 常规渲染、蒙皮动画、天空、阴影、透明/混合、LineRenderer、GPU 粒子模拟与粒子绘制、Screen UI、Bloom、ACES Tone Mapping 和最终 present；
16. 为每个 WebGPU pass 增加可判定的提交/输出诊断，并以独立 feature-toggle A/B 帧、固定相机像素区域和 Vulkan 参考帧比较证明效果确实贡献到 backbuffer；
17. 在桌面 Chromium、Android Chromium 和至少一组不同 WebGPU adapter/driver 上运行 040 项目全功能回归。

#### WebGPU 与 Vulkan 可见结果对齐 Gate

- Vulkan 是当前参考实现，但 WebGPU 不复制第二套场景语义。共享层负责相机、材质、灯光、动画、粒子图、LineRenderer、后处理配置和 RenderGraph 意图；Vulkan/WebGPU 后端只处理 API 所需的资源、管线和同步差异；
- 建立机器可读 capability/inventory，至少记录每个 Vulkan 产品 pass 在 WebGPU 上的状态：`equivalent`、`different-but-validated` 或 `unsupported`。040 所用能力不得保留 `unsupported`；
- 粒子 Gate 同时要求仿真推进和可见粒子像素贡献；LineRenderer 同时要求几何状态、实际 draw 与轨迹像素；Bloom/ACES 同时要求 pass 执行、输入输出纹理有效以及开关前后预期区域产生可解释差异；
- 不能只凭“没有 WebGPU validation error”、组件运行标记、非黑像素比例或整个画面 hash 判定对齐。自动化需分别隔离天空、阴影、粒子、LineRenderer、动画、Bloom 和 ACES，固定曝光、相机、分辨率与时间采样，比较结构和局部像素；
- 浏览器限制导致的算法差异可以使用等价实现，但必须有视觉容差、性能与资源预算；不得静默删 pass、关闭效果或用 DOM/CSS 补画；
- 040 人工验收必须能在同一场景中直接看到粒子、球后轨迹、动画角色、阴影、天空、Bloom 光晕和 ACES 色调响应，并与 Windows/Linux Vulkan 参考画面保持相同构图与可解释的色彩容差。

Web Python Decision Gate：

- A：完整普通 gameplay 子集能在 CPython/Emscripten 上维护，继续该路线；
- B：静态模块或浏览器限制使一部分功能不可用，但可通过显式 capability/拒绝规则维持同一源码，继续受限 CPython 路线；
- C：只有在 A/B 都被可复现证据否决时，才设计 portable lowering；必须单独评审，不得在实现中静默切换。

退出条件：安装插件后 Web target 出现；040 项目生成可部署目录，在本机 server、桌面 Tier 1 浏览器、Android Chrome 和 iPhone/iPad Safari 真机完成同一可玩任务；WebGPU 对 040 使用的 Vulkan 可见能力 inventory 无 `unsupported`，天空、阴影、蒙皮动画、LineRenderer、粒子绘制、Bloom、ACES、Screen UI 和最终合成都通过隔离像素证据及人工对照；多点 pointer、capture/cancel、中文/emoji 输入、软键盘/地址栏 resize、安全区和页面切换恢复通过；控制台无未处理异常，刷新/离线缓存行为可解释。

当前收敛状态：桌面 040 平衡球 Web 产物和 Android 真机 Chromium 已能加载 CPython 3.13、GUID 资产、场景、PBR/粒子/FBX/音频并进入 Python gameplay。针对人工暴露的天空盒/阴影缺失、输入后黑屏和加载页错误，门禁已从 marker 升级为真实像素差异：浏览器在 gameplay activation 前后及输入后读取帧，分别验证 sky/shadow 的像素贡献、非黑像素比例和未处理异常；gameplay activation 已与 audio user gesture 解耦，输入后画面继续渲染。加载页已使用正式熔炉图标和进度条。整幅画面上下颠倒的根因是核心 Camera 已发布 Vulkan Y-flip 投影，而 WebGPU framebuffer 又执行一次坐标映射；修复只落在 Web host 边界，对场景和粒子 VP 乘以 Web clip-space 校正矩阵并重算 inverse。临时 DOM FPS overlay 已被移除：Python UI 现在通过平台无关 `RuntimeScreenUISubmission` 生成 draw list，由 `WebScreenUIRenderer` 在同一 WebGPU backbuffer 绘制，HTML 仅保留隐藏 IME/软键盘桥。Web Player 的默认 scheduler 已发布 native phase work，Jolt 的静态库与全部消费者共享 deterministic 编译契约，项目物理配置在 Jolt 创建前经窄 Web host API 应用；2 MB WASM stack 修复首个物理帧溢出。最新 revision 的桌面 W/↑ 与 Android Edge 146 原生触控均已完成 PlayerBall 实际位移，运行时 phase error=0。验收项目进一步基于公开组件状态报告 LineRenderer 非零轨迹和末端贴合误差、ParticleSystem GPU 驻留/播放/仿真时间、SkinnedMeshRenderer take 时长和原生动画时钟；Web smoke 可声明多个必须出现的诊断并 fail-closed。首轮运行由此暴露 Web host 粒子控制面与桌面不对称，补齐 state-preservation 查询后，同一次实际滚动中输入、Jolt、LineRenderer、粒子、动画和运行时零错误已共同通过。但最新人工验收明确发现 WebGPU 与 Vulkan 仍有显著可见差距，粒子、后处理等多项效果不可见；这直接否定了用运行状态标记推导视觉完成的做法，因此 Phase 6 的渲染 Gate 已重新打开。下一步先完成 render-feature inventory，并逐项隔离验证 LineRenderer、粒子绘制、Bloom、ACES、阴影、天空、动画与最终合成，再继续 Android Chromium、真实双指、系统 IME/软键盘 resize、Chrome 品牌、iOS Safari、缓存和移动视觉回归。

2026-08-31 复验：WebGPU 已具备方向光与点/聚光、程序天空、方向光阴影、HDR 中间目标、Bloom、ACES、粒子 draw、LineRenderer、原生 Screen UI、PBR/Unlit/Toon 和 alpha clip 的实际运行路径；浏览器 smoke 在 revision `e1ce7a8f857fade30115382c` 上验证真实 W 键同时进入 native/Python 输入状态并使 PlayerBall 水平位移约 6.98，且控制台无 WebGPU validation error。基础色纹理链已从材质 GUID 经 AssetRegistry 异步 staging、WebGPU texture/sampler/bind group 到逐 draw UV 采样贯通；新增的真实 256×256 PNG fixture 会按正式导入规则 Cook 为 BC3 sRGB，首次运行准确暴露 WebGPU 上传不支持该格式，现已通过与 adapter 扩展无关的 BC1/BC3→RGBA8 回退解码完成上传，revision `a09ba7145ba7ee1eb41227ca` 同时出现 `INFERNUX_WEB_MATERIAL_TEXTURE_READY` 且无 validation error。尚未关闭的核心差距是法线/金属度/光滑度/AO/发光贴图、BC4/BC5/BC6/BC7 与平台压缩格式策略、材质采样参数和 mip 链、自定义 shader 的通用转换、局部光阴影、区域光和完整 RenderStack pass inventory；带纹理 fixture 目前证明了真实资源上传和 draw 绑定，但仍不能替代 Vulkan/WebGPU 固定相机局部像素对照。

2026-08-31 多贴图复验：WebSceneRenderer 已从单一基础色 bind group 扩展为 base color、metallic、smoothness、AO、normal 五组独立 texture/sampler；逐 draw 绑定保留各自 wrap、filter、anisotropy 与完整 mip 链，normal map 使用 mesh tangent/handedness/normalScale 构建 TBN。CPU portable fallback 现覆盖 BC1/BC3/BC4/BC5 的全部 mip，避免依赖浏览器 adapter 的 BC 扩展。真实 fixture 将五个不同 GUID 分别 Cook 为 BC3 sRGB、三张线性 RGBA8 和 BC5 normal，其中四张 mip 贴图均为 256×256/9 mips/trilinear；浏览器 revision `06c21c88895d5b745abf67a0` 五个 GUID 全部报告 READY，天空、阴影、粒子、Bloom/ACES、Screen UI、LineRenderer、动画同时就绪，WebGPU error 为 0。首次实跑还发现并修复 anisotropy>1 时 mipmap filter 非 Linear 导致 sampler/bind group/command buffer 连锁失效的问题。基础 PBR 多贴图、mip 与采样器语义可从“未实现”移出；仍未关闭的是 emission map、BC6/BC7 与平台压缩策略、自定义 shader 通用转换、局部光阴影、区域光，以及 Vulkan/WebGPU 固定相机的隔离像素对照。

2026-08-31 后处理与默认粒子复验：提交 `beb92869` 移除了 Web 粒子片元里宿主强加的径向 coverage/discard，默认粒子恢复为方形；WebPostProcessRenderer 从固定单 pass 改为读取 Cook 后初始场景的 RenderStack，按 GUID 展开 Effect/EffectGroup，执行 soft-knee 预筛选、13-tap 多级降采样、9-tap scatter 上采样、项目 tint/intensity 合成和 None/Reinhard/ACES 色调映射。revision `3f49b7f161a8c69135b7fd8a` 实跑报告 `bloom=1 iterations=5 tonemapping=2`，场景 59,691 vertices / 163,284 indices / 15 draws、程序天空、2048 阴影、粒子 draw、LineRenderer 与动画同时 ready，WebGPU validation error 和未处理异常均为 0；自动输入验收中 native/Python W 状态成立，PlayerBall 位移约 6.98。人物角色颜色、Bloom 主观强度及最终色调仍须与 Vulkan 固定相机做人工/像素对照；WebSceneRenderer 仍是独立 WGSL PBR 路径，因此此项不宣告通用渲染对齐完成。

2026-08-31 间接光复验：提交 `233c7969` 将 WebGPU 的 roughness-aware indirect Fresnel 改为与 `pbr.glsl` 完全相同的白色 grazing target，并让 horizon occlusion 使用插值几何法线而不是法线贴图扰动后的 shading normal。重新 Cook/编译得到 revision `5fcc8e57be33d7b5580a4962`；浏览器闭环中 Web 场景、程序天空、2048 阴影、粒子、五级 Bloom、ACES、LineRenderer、动画和 Screen UI 同时 ready，W 输入使 PlayerBall 水平位移约 7.10，phase error 和未处理异常均为 0。FBX 检查确认验收人物不依赖外部贴图，颜色来自模型内嵌的两个材质；剩余人物色差必须通过固定相机最终画面对照定位，不能归因于缺失外部纹理。

2026-08-31 跨后端帧证据：提交 `459f650a` 将 Player 已有的 token-authenticated engine render-target capture 接入 Windows smoke，并让 Web smoke 可在真实输入前输出同尺寸 Canvas 帧；新增比较器按 RGB 均值、亮度均值/百分位/直方图和空间 tile 统计执行 fail-closed。首次捕获误把 Web 输入后帧与 Vulkan 输入前帧比较，时序修正后，同一 Start 场景的平均亮度差为 0.00053、RGB 均值最大差 0.00132、亮度百分位最大差 0.00364、直方图距离 0.0604、最差 4×4 tile 亮度差 0.00535，全部通过收紧后的门禁。该证据证明当前全局曝光、色调和空间亮度分布已接近，但人物等小面积对象仍需局部 crop/人工对照，不能用全帧均值替代局部材质验收。

当前桌面 040 项目已用提交 `7e8bc01d` 重建为 revision `3628774d4f9e4082d1b06e98`；自动 Web smoke 重新验证 native/Python W 状态、Jolt 位移约 7.04、phase error=0、右键菜单拦截、键盘焦点、Pointer/Text/VisualViewport bridge，以及粒子绘制、Bloom/ACES、Screen UI、LineRenderer、动画全部同时就绪。

2026-09-01 当前工作区复验：revision `e8815606d4b98c0cb6657280` 在桌面 1440×900 viewport 下按项目声明保持 1280×720 游戏分辨率并居中，真实 W 键同时到达 native/Python 输入，PlayerBall 位移约 7.01；412×915 移动 viewport 下 Canvas 以 16:9 缩放并垂直居中，原生触控左摇杆令 PlayerBall 位移约 7.06。两轮均验证天空、阴影、Screen UI、LineRenderer、粒子、动画、音频、HDR `rgba16float`、五级 Bloom 与 ACES 同时 ready，右键菜单被拦截，输入后画面继续运行且无 phase/WebGPU/fatal error。该证据关闭当前 Chromium 的键盘/单指触控与窗口分辨率适配回归，但不替代真实双指、IME/软键盘、浏览器品牌矩阵和 Vulkan/WebGPU 局部像素对照。

2026-09-01 原生双触点复验：Web Development Player revision `7435b76e2b73cf8f04266f08` 从当前 Python 3.13/WebGPU 源码完整重建 77.98 秒并通过 package audit，040 必需能力的 `open_parity_gates` 为空。412×915、DPR=2 的 Chromium 移动上下文中，CDP 从浏览器输入边界同时投递两根真实 touch contact；Python 公共 `Input.touch_count` 从 0 变为 2，`Input.get_touch()` 返回两个稳定且不同的 `finger_id`，主触点恰好一个，两根手指归一化位移均为 0.113，cancel 后触点数恢复为 0。同轮左摇杆使 PlayerBall 水平位移 7.12，phase error=0，天空、方向阴影、Screen UI、HDR 粒子、Bloom/ACES、LineRenderer、动画、音频、右键拦截、中文/emoji composition 与 page hide/show 均通过，输入后画面保持非黑。该 fail-closed Gate 已加入 `platform-player.yml`，取代此前只在页面内构造 `PointerEvent` 的弱双指证据；仍未关闭的移动 Web 项是物理 Android 浏览器双指、系统软键盘/地址栏 `VisualViewport` 变化、Chrome 品牌矩阵及 iOS Safari。

2026-09-01 CI 基准闭环：`tests/fixtures/multiplatform_player` 不再只证明 Camera/Mesh 的最低启动能力，而是显式包含方向光阴影、三个分离投影体、接收地面以及由公共 Python API 创建的 Screen UI。revision `4010316d588672d826f8eca5` 的 Development Player 完整重建 73.98 秒；在 412×915、DPR=2 的 CI 同构环境中，真实双触点 Gate 通过，阴影开关产生的像素变化率为 `0.342%`、平均绝对差为 `0.000593`，同时 Screen UI、天空、键盘、文本组合、右键拦截、音频、生命周期与输入后非黑帧全部通过。该结果证明工作流中的阴影和 UI 检查不再依赖平衡球大场景偶然具备的内容，也没有通过 `skip` 或降低阈值放行。

2026-09-01 固定帧复验：Windows Vulkan Development Player 通过 token-authenticated 引擎渲染目标在固定 `1/60 s`、第 120 个 gameplay frame 暂停并输出 1920×1080 参考帧；WebGPU 在同一场景、时间、输出尺寸下由 Canvas 输出候选帧。比较器已改为禁止隐式缩放：尺寸不同必须显式允许等比例缩放，宽高比不同直接失败；同时新增逐像素平均绝对 RGB 误差、RMSE、P95 误差和变化像素比例。两端全画面门禁通过：平均亮度差 `0.00135`、平均绝对 RGB 误差 `0.00291`、RMSE `0.02399`、P95 误差 `0.01176`、超过 `8/255` 的像素比例 `2.54%`。两次独立复跑证明固定帧本身确定：WebGPU 逐像素完全一致；Vulkan 平均绝对 RGB 误差约 `1.17e-6`，LineRenderer/球区域完全一致。局部门禁仍保持失败并阻止关闭 Phase 6：粒子区域平均亮度偏高 `0.01743`、平均绝对 RGB 误差 `0.01958`、变化像素比例 `26.06%`；角色区域平均亮度偏高 `0.01051`；Bloom/发光区域平均绝对 RGB 误差 `0.01090`、变化像素比例 `22.19%`；LineRenderer/球区域平均绝对 RGB 误差仅 `0.00493`、变化像素比例 `2.39%`，但深色直方图距离 `0.10696` 略超 `0.10`。下一步先隔离 HDR scene color 与 Bloom 输出，区分粒子材质/阴影环境光差异和后处理差异，不通过放宽阈值关闭 Gate。证据位于桌面 040 项目的 `Evidence/*fixed-frame120*20260901.json`。

2026-09-01 Web 启动契约收敛：沿真实调用链确认旧 bootstrap 在 C++ WebGPU 粒子运行时建立前就构造 `PlayerRuntimeSession` 并加载场景，导致 ParticleGraph AOT 只能依赖初始化时序偶然成功。现在启动被拆为唯一的两阶段主路径：先建立 GUID AssetDatabase、类型目录、BuildSettings 与初始场景引用，只在 `infernux_web_ready()` 被 C++ 调用后才实例化 session 和场景组件；RenderStack 查询只允许读取前一阶段的资产契约。Web host 初始化粒子运行时失败、缺失 Python ready 回调或 ready 回调报错都会立即终止，不再无粒子继续启动。最终 Development 产物 revision `886e78cba390bb8012dac102` 在固定 1280×720、DPR=1、第 120 帧复验中按顺序报告粒子运行时 ready（诊断序号 28）后场景 loading（序号 31），`MSAA=4`、HDR `rgba16float`、五级 Bloom 与 ACES 同时生效；粒子四态 A/B 的 Bloom halo 变化比例为 `2.03993%`，确认粒子真实进入 HDR/Bloom 链。实时 W 输入同时到达 native/Python，PlayerBall 水平位移约 `6.99`，runtime phase error、页面错误和控制台未处理错误均为 0。WebAudio 的 `play_on_awake` 也已收敛到现有的唯一延迟主路径：AudioSource 在场景加载时只注册，可信用户手势解锁设备后由 AudioEngine 启动，不再先做一次必然失败的 voice 创建；更新后的产物在固定帧阶段无预激活音频告警，实时阶段仍得到一个 active voice，smoke 已把该告警列为禁止诊断。证据为 `Evidence/web-fail-fast-build-20260901.json`、`Evidence/web-fail-fast-fixed-frame-20260901.json`、`Evidence/web-fail-fast-input-20260901.json`、`Evidence/web-audio-contract-build-20260901.json`、`Evidence/web-audio-contract-fixed-frame-gated-20260901.json` 与 `Evidence/web-audio-contract-input-gated-20260901.json`。

2026-09-01 Runtime UI/Editor 边界复验：当前源码首次联合 smoke 在场景加载时确定性失败，调用链为公开 `inx.ui` → `Infernux.ui.ui_render_dispatch` → Editor `Theme` → Web 原生表面不存在的编辑器主题注册表；这不是 Web 应补的能力。现已删除运行时分派对 Editor Theme 的顶层依赖，文本与按钮字号、行高、字距和颜色只读取 `UIText`/`UIButton` 的序列化字段这一唯一数据源；编辑器占位图所需 Theme 仅在 editor renderer 真正执行时加载，没有增加 Web 伪接口或默认值 fallback。隔离子进程回归会主动拒绝任何 `Infernux.engine.ui.theme` 导入并验证公开 `inx.ui` 可用。新 Development Player 在 82.70 秒重建为 revision `b312e7f32d0954d451c9b0a0`，WebGPU capability inventory 无 open gate；1440×900 viewport 中按项目声明居中呈现 1280×720，真实 W 键同时到达 native/Python，PlayerBall 水平位移 `7.0948`，天空、阴影、Screen UI、LineRenderer、GPU 粒子、FBX 动画和一个 active audio voice 同时通过，右键菜单被禁止，runtime phase error 与未处理页面错误均为 0。引擎 Python 全量回归为 **4,845 passed / 2 skipped**。证据为 `Evidence/web-runtime-ui-boundary-build-20260901.json` 与 `Evidence/web-runtime-ui-boundary-smoke-20260901.json`。

同一 Runtime UI 边界修复随后由当前源码分别重建 Windows Vulkan 与 Ubuntu 24.04 Linux Vulkan Development Player，而不是只复验 Web。Windows 受控 smoke 中真实 W scancode 令 `PlayerBall` 沿 z 轴移动 `4.2738`，普通 MeshRenderer 为 2609 顶点并持有有效材质 GUID，LineRenderer 为 67 点，GPU ParticleSystem 常驻、播放且时钟推进至 `1.3458 s`，FBX `mixamo.com` 动画时钟同步推进，fatal/VUID 为 0。Linux 双 L20 真机使用 NVIDIA Vulkan ICD、Khronos Validation 和隔离 Xvfb 对称复验：当前源码构建在 `17.46 s` 内完成且因 Runtime UI owner 变化确定性重建 Runtime Pack；W 输入令球体移动 `4.2968`，LineRenderer 为 68 点，粒子时钟推进至 `5.1033 s`，FBX 动画时钟推进至 `1.2906 s`，fatal/VUID 为 0。两端 Player 与 Linux Xvfb 均已在验收后关闭。证据为 `Evidence/windows-runtime-ui-boundary-build-20260901.json`、`Evidence/windows-runtime-ui-boundary-smoke-20260901.json`、`Evidence/linux-runtime-ui-boundary-build-20260901.json` 与 `Evidence/linux-runtime-ui-boundary-smoke-20260901.json`；Android 当前源码 APK 因 HyperOS USB 安装确认暂不可人工操作而保持未关闭，不以旧包或模拟器结果替代。

当前源码的固定帧证据也已重新冻结，取代上文较早产物中仍然失败的局部 WebGPU/Vulkan 对照。Windows Vulkan 与 WebGPU 都在项目声明的 1280×720、DPR=1、固定 `1/60 s`、第 120 gameplay frame 暂停后取自真实渲染目标，没有隐式裁剪或缩放。全图平均亮度差为 `0.000479`、平均绝对 RGB 误差为 `0.001490`、超过 `8/255` 的像素比例为 `0.577%`；scene、粒子、角色、LineRenderer/球和 Bloom 五个局部区域全部通过原有阈值，其中粒子区域平均绝对 RGB 误差为 `0.000244`、Bloom 区域为 `0.000232`。两端各自独立复跑也通过更严格的确定性阈值：Vulkan 两帧平均绝对 RGB 误差为 `1.96e-6`，WebGPU 为 `9.33e-7`。因此当前 Chromium/WebGPU 对 Windows Vulkan 的这组固定场景像素 Gate 已关闭；它不替代其它浏览器/设备以及最终人工画面验收。证据为 `Evidence/windows-vulkan-runtime-ui-fixed-frame120-20260901.json`、`Evidence/webgpu-runtime-ui-fixed-frame120-20260901.json`、`Evidence/vulkan-webgpu-runtime-ui-fixed-frame120-regions-20260901.json`、`Evidence/windows-vulkan-runtime-ui-fixed-frame120-repeatability-20260901.json` 与 `Evidence/webgpu-runtime-ui-fixed-frame120-repeatability-20260901.json`。

### Phase 7：四端同项目兼容 Gate

统一测试步骤：

1. 从干净安装创建/打开桌面 040 项目；
2. 安装 Android/Web 官方平台包；
3. 在 Windows/Linux Editor 编辑、保存并 Play；
4. 构建 Windows/Linux/Android/Web；
5. 启动每个产物并完成同一输入→物理→UI→场景→音频任务；
6. 记录结构化状态轨迹、GUID 资产目录 revision 和 capability report；
7. 对允许的平台差异按声明容差比较；
8. 卸载/重装平台包并重复目标发现和构建；
9. 从无缓存环境复跑；
10. 使用 MCP 从干净项目重复执行至少一次创建/编辑/Play/四端构建/结果检查主路径；
11. 汇总包体、启动、构建耗时、MCP 调用成本和失败分类。

退出条件：四端全部通过；任何一个平台缺席都不能关闭 0.4.0 Gate。

### Phase 8：四端平台插件化、Hub 共享缓存与 Release 分发

本 Phase 必须在 Phase 7 的 Linux、Android、Web 兼容卡点收敛后开始。此前允许 Windows/Linux 暂时继续使用核心
`DesktopPlatformExporter`，Android/Web 继续使用仓库内官方插件源码；不得在渲染、输入、生命周期、安装、启动或性能尚未成立时，用大规模插件系统重构替代平台调试。

执行顺序：

1. 用 Phase 7 已通过的四端行为测试冻结 `PlatformExporter`、Cook、RuntimePayload、doctor、progress、cancel、audit 和 smoke contract；
2. 建立 `infernux/platform-windows`、`infernux/platform-linux`、`infernux/platform-android`、`infernux/platform-web` 四个独立 InxPackage owner，把 Player exporter、平台模板、目标打包与 toolchain manifest 移出核心；Editor/Headless 的 Windows/Linux 原生发行仍归 engine wheel；
3. 修改 Windows/Linux wheel 组装：两者的 `python/Infernux/resources` 都只写入 MCP；项目 bootstrap 只把 MCP 定义为必须安装集合。官方目录独立列出四个平台包，Build Settings 在没有对应 exporter 时显示“需要下载插件”并打开准确条目；资产身份和冲突检测只由 GUID owner 负责，不生成整包哈希；
4. 把官方注册表改成远程、可缓存的兼容性验证目录。host wheel 只保留 MCP、目录 bootstrap 与 schema，不携带任何平台包；目录中的“官方”只表示 Infernux 项目实际验证过该 reference/version/engine 组合，不限制用户通过 `.inxpkg`、Git、其它托管平台或内网自由分发。目录离线或损坏只令远程发现不可用，不阻断 Editor、MCP、本地包导入或已缓存插件；
5. Hub 拥有唯一的共享 package cache 与 toolchain cache。Download 只把指定版本预载到 Hub；Re-download 重新获取到临时文件，按当前 InxPackage 结构完成一次解析后原子替换缓存；Import 才把 reference/version/cache source 写入项目 lock 并激活。Editor 不再把远程包复制到每个项目的 `Library/InxPackageCache`；多个项目和多个兼容 Infernux 版本可共享同一缓存项，不为缓存再建立 SHA blob 身份；
6. 插件窗口提供 All、Installed、Official Registry 等明确来源视图；Official Registry 身份只来自官方兼容性目录的收录，不接受包内自报，也不成为其它分发方式的门禁。条目显示类别、reference、版本列表、Infernux 兼容区间、host/target/ABI、下载/缓存/安装状态，并提供 Download、Re-download、Import、Uninstall；GitHub/其它 URL、本地 `.inxpkg` 与 PyPI 搜索在视觉和数据模型上分离；
7. 构建窗口消费同一 `PlatformSupportCatalog`，可以展示未安装平台，但未安装目录项不是 `BuildTarget`。它显示“当前不支持，需要安装平台插件”，并跳转插件窗口定位 reference；安装与 lifecycle 注册成功后才可构建。CLI/MCP 返回结构化 `platform_plugin_required`，不静默下载；
8. 所有目录与 Git 安装在下载前按当前 `ENGINE_VERSION` 解析版本。resolver 只选择 `package/inx_package.json` 的 `engine` 覆盖当前引擎的 release，默认取最高稳定 SemVer；没有兼容版本时禁止安装并列出原因，不能改抓 HEAD 绕过约束；
9. GitHub URL 使用 Release-first：读取非 draft release 的机器 manifest 和 `.inxpkg`，匹配 tag、包内 version/reference/engine 与附件名后放入 Hub，不为 Release 再做整包 SHA-256；只有仓库完全没有 Infernux release 附件时才 shallow-clone 默认分支，从源码生成临时包，并在 lock 中记录 commit SHA 与 `source snapshot`。同一仓库的一个 Release 可以用按 reference 命名的 manifest 同时承载多个插件；其它托管平台复用 provider contract；
10. 更新 `ChenlizheMe/infernux_plugin_template`：Git 仓库只把小写 `package/` 作为分发根，使用 `inx_package.json`、`runtime/`、`editor/` 与 `plugin_pages/`；仓库外层可自由放置 CMake、Gradle、Cargo、npm、源码和 README。根部提供只依赖 Python 标准库的 `package.py`，不得 import Infernux；tag `vX.Y.Z` 必须等于 manifest version，CI 确定性双构建并上传 `.inxpkg` 与机器 manifest，不生成额外整包摘要；
11. 从通用核心源码安装面删除直接编译的四平台 exporter/template，并从所有 host wheel 删除四个平台 artifact；只保留 MCP。更新 wheel package audit、built-in package bootstrap、官方目录、Hub、插件 UI、构建 UI、MCP、README 与 CI；验证卸载一个项目不会删除其它项目仍引用的 Hub blob/toolchain，清缓存也不能破坏活动引用；
12. 用 040 项目从空 Hub cache 重走“发现→下载→导入→四端构建”，再从已有缓存离线重走；分别验证 host 默认平台、其它按需平台、re-download、版本不兼容、Git release、无 release 时的显式源码快照来源、卸载/重装，以及不完整或不可解析缓存会失败并要求用户显式 Re-download；不做隐式修复或来源切换；
13. 建立插件混合资源 fixture：至少携带 `.mat`、vertex/fragment Shader、HTML/CSS/JS、未知扩展文件、`.wasm`、`.pyd` 及一个 editor-only 原生库。逐字节验证打包、安装、GUID 所有权、卸载和重装；`runtime/` 与普通资产进入 Player，`editor/` 与 `plugin_pages/` 不进入 Player，不能按扩展名猜测或遗漏文件；
14. 为“游戏内按钮打开插件携带网页”建立端到端测试：网页作为普通插件资产安装，运行时通过公共资产路径 API 解析，再通过可注入的平台 URL opener 打开。单元测试使用 observer 验证唯一规范路径/URL，平台 smoke 验证真实系统或浏览器行为；测试不得在 CI 中意外拉起外部浏览器。项目 `Assets` 内出现 `inx_package.json` 只代表正在开发的资产，不得被识别、安装或 preload 为插件。

2026-09-03 当前实现复验：四个 `infernux/platform-*` 已分别成为独立 InxPackage owner；当前合同进一步收敛为 Windows/Linux wheel 都只携带 MCP，四个平台包只保留官方目录记录并由用户按需下载。Download 与 Import 已分离，Hub 缓存以 `reference/version` 为主键，同一插件版本跨项目共享，不计算整包 SHA-256；Re-download 通过临时文件原子替换。GitHub provider 固定为 Release-first，按 reference/version/engine/tag/附件名选择最高兼容稳定版本；同一仓库的一个 Release 可用按 reference 命名的 manifest 同时承载多个插件。插件分发不设签名、整包哈希或官方批准门禁，Official Registry 只标记 Infernux 项目实际验证过的兼容版本。

官方包继续由显式目标按“原生→插件→wheel”顺序生成。正式 Windows cp313 wheel 已从该目标重新组装；在无 Hub、`PIP_NO_INDEX=1` 且只安装本地 wheel 的隔离 Python 3.13 环境中，新项目自动安装 `infernux/mcp` 与 `infernux/platform-windows`，只注册 `windows-x64` BuildTarget，同时仍能发现 Android/Linux/Web 官方条目；manager shutdown 后 exporter registry 为空。对应 wheel host bootstrap 结构化证据已归档于 `out/acceptance`。Ubuntu 双 L20 真机也从隔离 venv 中已安装的 0.4.0 wheel 启动，在 Xvfb 1600×900 + NVIDIA Vulkan 下完成打开、保存、Play 和退出且无 VUID、Traceback 或失败事件；验收进程均已关闭。

插件仓库与本地作者入口现已分离：Git 仓库只归档小写 `package/`，仓库 README、构建配置和独立标准库 `package.py` 留在外层；File Manager 选中的本地目录则永远直接作为包根，不猜测嵌套仓库结构。因而 `Packages/abc` 可不写 `inx_package.json`，导出为 `abc.inxpkg` 时自动得到默认 `name/reference=abc`；多选裸目录保留各自目录名并直接展开到 `Assets/Plugins`。混合 `.mat`、Shader、HTML/CSS/JS、未知文件、`.wasm`、`.pyd` 和 editor-only DLL 的打包/Player 路由已由回归锁定，插件安装会在事务提交后主动发布 runtime Python 变更，不再等待不可靠的 watcher。当前该入口及插件生命周期相关定向回归为 **128 passed, 2 skipped**。插件网页也已形成真实的单元端到端链：安装后的 HTML 由 `Application.asset_path()` 解析，`UIButton` 点击把唯一规范 URL 交给可注入的 opener，生产实现直接调用 SDL 平台 handler，CI 不拉起浏览器；Android/Web 的真实系统或浏览器 smoke 证据仍待补齐。`ChenlizheMe/infernux_plugin_template` 已提交并推送到模板主仓，独立打包器确定性双构建及引擎读取互操作已通过；模板远端 Release Actions 尚待 tag 实跑。Hub 设置页现显示统一 Plugin Library 的位置、包数和占用，并能清理所有登记项目均未引用的版本；任何项目路径缺失、注册表损坏或缓存引用越界都会禁用删除，源码 `launcher.py` 与安装版共用 `INFERNUX_DATA_ROOT/Library/Plugins`，Hub/packaging 全量回归为 **206 passed**。官方插件迁移后的旧 `Editor/Runtime` 路径也已从测试夹具与真实 Player acceptance 入口清零；Linux CI 随后捕获到 preload 临时导入路径仍拼接旧大写目录的问题，现已统一为小写 `runtime/editor`，防止已安装插件意外从源码 checkout 导入并在卸载后残留模块。当前引擎 Python 全量回归为 **5086 passed, 4 skipped**，插件/原生安装卸载定向回归为 **91 passed, 1 skipped**。本 Phase 仍保持打开：远程官方目录和实际官方 Release 尚未部署，Hub 空缓存→下载→导入→离线复用尚未基于正式远程资产闭环。

2026-09-04 增加面向所有插件资源类型的 package-private StreamingAssets 边界，而不是为 Java 单独设计路径：`runtime/` 下的 JAR、JSON、Wasm、词表及带相对 include/import 的完整目录树在 Player 中逐字节保留，普通脚本用 `Application.package_path(reference, relative)`，生命周期脚本用 `PreloadContext.package_path(relative)` 获取当前目标上的真实只读路径；绝对路径、盘符、反斜杠 reference、`..` 越界和缺失内容均明确失败，可变数据只进入 `persistent_data_path()`。Player 编译 package Python 源码时把 preload 静态声明与 `.pyc` 路径写入运行时 registry，运行时只消费编译产物，不保留源码，也不导入 Editor 的 PanelRegistry。`multiplatform_player` fixture 已加入 package 内 TXT 与真实 `InxPreload`，Bootstrap 同时读取该文件、核对 preload 值并创建 `UIText`；Windows Development Player 已通过精确文本、输入、物理、LineRenderer 和零 fatal 验收，Web、Android 真机与 Linux 冻结提交证据仍在补齐。

退出条件：exporter/artifact/template 不再作为核心内建实现；所有 wheel 只内嵌并自动安装 MCP，Windows/Linux/Android/Web 都只有按需安装对应插件后才可构建；未安装时 Build Settings 明确给出下载入口；四个平台都由相同 lifecycle 注册并保持 Phase 7 行为；官方/Git release 按引擎版本正确匹配，Hub 缓存跨项目共享且可离线复用；插件模板能够在没有 Infernux 环境时自动发布可验证 InxPackage；混合资源与运行时网页用例通过 Editor、Player、安装和卸载合同。

### Phase 9：发布证据和维护闭环

交付：

- PR 快速门禁：Windows/Linux configure + native/core/headless contract；只运行不依赖真实 GPU 的稳定集合；
- PR 平台门禁：Android compile/package + emulator smoke、Web compile/link + 固定 Chromium WebGPU smoke；可按路径触发，但 release 分支必须全跑；
- Nightly：干净缓存四端构建、Android 生命周期循环、浏览器矩阵、GUI 启动、包审计和耗时/体积趋势；
- Release candidate：Windows/Linux 可下载产物、Android APK/AAB、Web deploy bundle、arm64 真机记录和完整 provenance；
- `evidence-manifest.json`；
- 四端产物 hash 与构建环境；
- CI 链接、设备/浏览器矩阵；
- 已知限制和 capability matrix；
- exporter 插件独立版本与兼容区间；
- 环境搭建、调试、崩溃收集和部署文档；
- 中英文 README、官网平台页、`pyproject.toml` classifier、Preset 文档与 Release notes 同步更新；
- 失败数据和 deferred items；
- 官网只在公开产物可下载后把对应能力从 planned 改为 available。

CI 结构约束：Ubuntu runner 是 Linux 正式门禁，WSL2 只作本机反馈；Android/Web 重任务可以从普通 PR 快速反馈中分层，但不能长期只靠手动 workflow；所有 CI 使用与本地相同的 Preset/workflow 和 stage/package contract，不维护 Actions 专用编译命令。缓存仅优化下载与编译，不得成为测试通过的前置条件。

当前收敛状态：Windows/Linux 既有 Preset CI 已补入全量 Python/平台 contract suite，并与 CTest 一起输出 JUnit artifact；当前分支本机全量回归为 Python 4801 passed / 2 skipped、Windows release CTest 58/58 passed。通用 `build_evidence_manifest.py` 已能把同一 evidence root 下的文件/目录产物、build/smoke JSON、Git commit、环境、体积与确定性 hash 绑定为 `evidence-manifest.json`，正式候选可用 `--require-clean` 拒绝脏工作区。该工具已在桌面 040 项目的 Windows Player、Android arm64 APK、Web Player 与三份验收 JSON 上实际生成 manifest。`build_player.py` 的证据现只完整保留阶段事件，CMake/Gradle 原始输出改为计数摘要与最多 300 行日志尾；同一 AAB 增量构建的 JSON 从约 2.8 MB 降至约 25 KB，同时保留 298 个事件的 phase counts 和 249 行诊断尾。Android CPython 前缀现已具备强制 provenance/integrity manifest，记录 ABI、CPython/最终最低 API、NDK、官方源码哈希、wheel 哈希及完整 payload tree hash；Linux 一键准备脚本固定 CPython 3.13.15、NumPy 2.5.2、cibuildwheel 4.2.0 和 NumPy cross-file，从源码生成并原子发布前缀。本机 arm64 端到端生成得到 895 个 payload 文件、约 69.6 MB；最新统一 exporter 首次完整配置与编译在 230.8 秒内生成 63.5 MB debug APK并通过 package audit，随后 arm64 真机自动验收在约 20.5 秒内完成 gameplay ready、跨帧 touchscreen 输入、3200×1440 横屏 Surface、Back 与两轮 resume，fatal=0、abandoned buffer=0。托管平台工作流现已复用仓库内同一 `build_player.py`、Android/Web 工具链准备脚本和 smoke 入口，包含 API 36 AOSP x86_64 emulator、固定 Playwright Chromium、缓存、失败证据上传、定时运行和进程清理；本地 workflow/CLI 契约测试已通过，尚未以远端 Actions 运行结果计作完成证据。Web 的最小同项目 fixture 已在本机由真实 exporter 构建并通过首帧、触摸、中文 composition、VisualViewport、page hide/show 与音频激活的浏览器闭环。剩余 CI 核心缺口是首次远端运行后的环境差异修复、PR/Nightly/RC 成本分层和公开产物 provenance。

### Phase 10：Windows Editor 高 DPI 与多显示器自适应

本 Phase 在平台构建、真机、插件交付和 CI 工作收敛后执行，随后进入 Phase 11 的 Python 开发界面迁移。基准画面为 Windows 1920×1080、96 DPI、100% 缩放下当前已确认的 Editor 视觉：其它设备应保持相同的逻辑字号、间距、控件密度和面板比例，而不是按物理像素复制窗口，也不能交给 Windows 对整张窗口做模糊位图拉伸。

执行顺序：

1. 在创建任何原生窗口前声明并验证 `Per-Monitor V2` DPI awareness；打包后的 Editor、源码启动和 Hub 启动路径必须一致，不能由启动方式决定是否清晰；
2. 明确区分物理像素、DIP/逻辑坐标、SDL window size、drawable/framebuffer size 和 ImGui 坐标；渲染 viewport、鼠标/触笔命中、拖拽、弹窗位置、窗口持久化和截图区域只能在规定边界转换一次，禁止 double scaling；
3. 建立统一 Editor UI scale 服务，读取当前窗口所在显示器的实际 DPI/display scale，以 96 DPI=1.0 为基准驱动字体 atlas、图标、行高、padding、margin、分隔线、最小控件尺寸和停靠区最小尺寸；业务 panel 不得散落读取 Windows DPI 或自行乘比例；
4. 字体与矢量/高分辨率图标按目标 scale 重建或选取合适资源，避免 150%/250% 下半像素抖动、字形模糊、图标发虚和线宽不一致；主题颜色、全局字号层级和 NASA-punk 视觉保持不变；
5. 响应 `WM_DPICHANGED`/SDL 对应 display-scale 与 display-change 事件；窗口从 100% 显示器拖到 150%/200%/250% 显示器时，无需重启即可原子更新字体和布局，采用系统建议窗口矩形，避免一帧错位、内容跳变、输入偏移或 dock layout 损坏；
6. 对 Scene、Game、Inspector、Hierarchy、FileManager、Console、Package Manager、Build、Physics Interaction、Preferences、曲线/Gradient 编辑器、文件对话框和各种 modal 逐一消除硬编码像素假设；低逻辑分辨率时允许内容区滚动或受控折叠，但按钮、底栏和关键操作不能被裁掉；
7. 窗口布局持久化保存逻辑尺寸、显示器身份和必要的 scale 元数据；显示器缺失、缩放变化或旧配置越界时恢复到当前工作区可见范围，不把旧物理像素坐标直接套到新 DPI；
8. 建立可重复的 DPI 验收工具：至少覆盖 1920×1080@100%、2560×1440@150%、3840×2160@150%/200%/250%，以及一组 100%↔200% 的混合 DPI 跨屏拖动；记录窗口客户区、framebuffer、字体 scale、关键控件逻辑矩形和输入命中结果；
9. 自动对比使用归一化到 96 DPI 的布局几何和文本基线，不以逐物理像素截图相等作为错误标准；最终再由人工肉眼确认清晰度、密度、边距、行高、dock 操作和跨屏过程没有明显跳变；
10. 将 Per-Monitor DPI smoke 加入 Windows CI 可执行的 contract 层；真实混合显示器和 4K/250% 结果进入 Release evidence。完成后同步 README/安装文档中对 Windows 缩放与多显示器的支持口径。

当前推进状态：SDL 原生窗口已经使用 `SDL_WINDOW_HIGH_PIXEL_DENSITY`，源码 Editor/Player 会在 SDL video 初始化前显式要求 `permonitorv2`，初始化后再通过 Win32 查询线程 DPI context；系统未实际授予 Per-Monitor V2 时直接终止，不降级到 system-aware 或 bitmap scaling。Windows Player 清单也只声明 `PerMonitorV2`，不再列出较低 DPI 模式。独立原生进程门禁已用仓库内同一 SDL 验证该策略真实生效，并与 100%/150%/200%/250% framebuffer 密度矩阵一同进入 CTest。Editor GUI 现从“启动时只读取一次 display scale”改为每次 GUI 构建前检测当前显示器；scale 改变时从未缩放的统一基础 style 原子重建全部 ImGui 尺寸、保留主题颜色、按新 scale 重建字体，并同步唯一的 `InxGUIContext` DPI 服务，避免跨屏时累乘旧尺寸。无效 display scale 不再静默替换为 1.0，Python 与原生公开查询也不会在 GUI 未初始化时伪造 100% 缩放。公共 modal、底部 action row、Curve/Gradient 编辑器、原生状态栏、Panel 初始尺寸、Preferences、Game View 工具栏、Package Manager、InxPackage 导入、Build Settings、命令面板、Tags/Layers、Physics Interaction/物理碰撞矩阵、UI Editor chrome、Console、Hierarchy、Inspector、Project/FileManager，以及 Scene 顶层 gizmo、粒子预览和 LineRenderer authoring overlay 的 authored metrics 已接入统一 DPI 服务；Inspector 的原生 checkbox/section-header 能力缺失时会直接报错，不再切换到另一套 Python/普通 ImGui 表面。系统文件对话框由当前平台的原生窗口系统管理缩放，Editor 自有 Save As/确认窗口统一经过 DPI-aware modal 服务。布局持久化只写入当前精确结构的 display-scale 元数据；结构不完整或不同 DPI 的布局会被明确拒绝，不做猜测性物理像素变换，浮动窗口继续由当前工作区可见范围约束。最新 Windows Release Runtime 编译、全量 4794 passed/2 skipped Python 测试和 58/58 CTest 均通过。Phase 10 仍保持打开：本机只有 1920×1080@100% 单屏，仍需真实混合 DPI/4K 设备矩阵、跨屏输入命中与 framebuffer evidence、dock 布局跨屏人工验收，以及其它低频 Editor surface 中残留硬编码物理像素的最终审计。

退出条件：同一 Editor 构建在上述 DPI/分辨率矩阵中无需用户修改主题或字体即可保持与 1920×1080@100% 基准一致的视觉密度；文本和图标清晰；Game/Scene 渲染分辨率正确；点击、拖拽、弹窗、dock、窗口保存恢复均无坐标偏移；跨屏切换不重启、不闪烁、不裁切，并有自动记录与真实设备人工证据。

### Phase 11：统一 Python 导入界面、代码模板与公开示例

0.4.0 倒数第二个开发任务是把面向用户的 Python 写法统一为显式命名空间：`import infernux as inx`。不再由模板、示例或文档推荐 `from Infernux import *`、`from infernux import *`，也不依赖 Windows 文件系统对包名大小写不敏感的行为。

执行顺序：

1. 建立真实的小写 `infernux` 公共入口，并在 Windows、Linux、Android 与 Web 的 Python 3.13 环境中验证；它不能只是 Windows 上碰巧可用的大小写别名，也不能复制第二份运行时状态；
2. 由主包维护可审计的公共导出记录：顶层 `__all__`、类型存根、符号来源、弃用状态和按平台可用性保持一致；常用核心类型、数学类型、组件、输入、时间、屏幕、资源与调试接口能够通过稳定的 `inx.<Name>` 访问；不适合顶层展开的领域保留为 `inx.ui`、`inx.physics`、`inx.renderstack` 等明确子命名空间；
3. 明确 `infernux` 与现有内部 `Infernux` 的边界。用户代码只依赖小写公共入口；引擎内部可在迁移期继续使用实现包，但不得让两种导入产生两套单例、重复组件注册、类型身份不等或序列化 type ID 漂移；
4. 更新新项目脚本模板、组件模板、菜单生成代码、教程项目、测试 fixture、插件模板和 MCP 生成脚本，默认生成 `import infernux as inx`，示例写成 `class Player(inx.InxComponent)`、`inx.Vector3(...)` 等显式形式；
5. 全仓扫描并清理公开表面的星号导入和旧推荐写法，覆盖 README/README-zh、官网、文档、Wiki 之外由本计划授权的介绍文件、代码片段、打包模板及错误提示；Wiki 保持独立维护，不在本任务中自动改写；
6. 为公共入口增加导出快照与跨平台 import contract：重复导入幂等、`inx.InxComponent is Infernux.InxComponent`、组件注册不重复、pickle/序列化身份稳定、Player 裁剪后所需符号仍存在、Editor-only 符号不会误进 Player；
7. 在桌面 040 平衡球项目中把验收脚本迁移到新写法，重新构建 Windows/Linux/Android/Web 四端，并确认脚本编译、预载、热重载、插件代码、MCP authoring 和最终 Player 行为一致；
8. 增加 lint/contract 门禁，禁止新模板与公开文档重新引入星号导入；对确有必要的内部兼容代码使用窄范围白名单并说明原因。

当前推进状态：已经增加真实的顶层 `python/infernux.py`，它只转发现有 `Infernux` 公共对象，不复制运行时、组件类型或单例；setuptools、CMake install 和 wheel build 均显式交付该入口及 `python/infernux.pyi`。公共导出已补入 lifecycle/physics/resources/components 子命名空间以及 `InxPreload`/`PreloadContext`，脚本候选扫描与 preload 静态发现都识别 `import infernux as inx`，不会要求用户退回旧导入。新建脚本模板、官网入门页和全部 Gameplay 教程已经迁移；自定义 RenderPipeline、RenderEffect、RenderGraph 和 RenderStack 挂载点四份中英高级教程也已统一使用 `inx.renderstack`/`inx.rendergraph`，原先只存在于内部 discovery 模块的导入失败诊断已提升为带类型存根的 `inx.renderstack.discovery_import_failures()` 公共能力。文档 contract 现在阻止 README 和全部 Learn 教程重新导入大写实现包或使用星号导入，生成后的静态 HTML 也经过同一搜索校验；Wiki 保持独立维护。Android/Web exporter 会把同一运行时入口放入各自 Player 的 `site-packages`。桌面 040 项目的 `BalanceGame.py` 与 `CrossFbxAnimationProbe.py` 已迁移为小写入口。最新同一源码重建中，Windows Development Player 在 27.24 秒完成，隐藏式 token-authenticated smoke 在 5.86 秒内使 `PlayerBall` 沿 z 轴移动 4.77，并直接断言渲染提交有效、球体 `MeshRenderer` 有 2609 个顶点与有效材质 GUID、`LineRenderer` 有 72 个动态点、粒子已驻留/播放且无编译错误、FBX take 为 `mixamo.com` 且原生动画时钟推进。Ubuntu 24.04 双 L20 真机由显式 Linux PlayerHost 在 17.81 秒内重建相同项目；NVIDIA Vulkan ICD、Khronos Validation 与隔离 Xvfb 下 127 帧 smoke 使球移动 5.43，LineRenderer 为 77 点，粒子和动画断言与 Windows 一致，fatal/VUID=0，Player/Xvfb 均正常关闭。Android arm64 当前源码完整重建耗时 83.50 秒并生成 68.5 MB debug APK；真机自动安装后在 21.91 秒内完成一次真实触屏、虚拟摇杆/action、Unity 风格 touch API、3200×1440 横屏、Back 与两轮 Home→恢复，LineRenderer/粒子/动画标记全部出现，四次 Surface 创建稳定，fatal=0、abandoned buffer=0，应用随后强停。Web 当前源码完整重建耗时 80.80 秒；Chromium 闭环确认 Python/WASM、场景、首帧、天空盒、阴影、Screen UI、音频、键盘焦点、pointer/text/VisualViewport、右键菜单抑制和 page hide/show，真实 W 输入使球移动 7.14，LineRenderer 为 6 点、粒子时钟和 FBX 动画时钟推进，runtime phase error 和未处理异常均为 0。Windows 3.13 wheel 已真实按“原生→官方插件→wheel”路径重建，并在隔离目录安装验证 `infernux.py`、`infernux.pyi`、默认 MCP 包和 Windows 平台包同时存在，`inx.InxComponent is Infernux.InxComponent`、`inx.InxPreload is Infernux.InxPreload`，131 个公开导出可用。Linux 也已从当前同一源树生成 cp313 wheel，在远端 Ubuntu 的全新隔离环境中确认顶层 `.py/.pyi` 文件齐全、131 个公开导出可用，且 `InxComponent`、`InxPreload`、`AnimationCurve` 和 `physics` 均与实现包保持类型/模块身份；验证结束后临时 venv、源码覆盖和测试进程均已清理。本轮公共入口、扫描、preload 与文档 contract 定向测试 125 项通过。最新全量回归为引擎 Python 4804 passed/2 skipped、Hub/packaging 162 passed、Windows Release 增量原生构建成功且 CTest 58/58 通过；新增组件级验收查询与断言的定向测试另有 102 项通过。新增高级教程公共能力与文档门禁定向测试 35/35 通过，Learn 静态页与 Service Worker 完成确定性重建和 `--check`；除实际 MkDocs Wiki 重建外，Website Quality 的版本、PWA、国际化、性能、响应式与交互门禁均在本地通过，Wiki 文件未修改。Phase 11 仍保持打开：剩余公开介绍表面、更广的仓库级 lint 门禁，以及同一冻结提交（而非脏工作树快照）上的最终四端重建与人工画面对照尚未完成。

2026-09-01 新增组件级四端验收后再次执行全量回归：引擎 Python **4807 passed / 2 skipped**，Hub/packaging **162 passed**。上段较早的 4804 统计仅保留为该轮变更前的历史快照。

当前实现收口：新脚本模板、公开入口、类型存根、扫描/preload、桌面验收项目与公开文档的主路径已经统一为 `import infernux as inx`；公共 namespace、模板、脚本候选和四端 fixture 定向合同 123 项通过，本轮全量回归也未出现第二份类型身份或组件注册。Phase 11 不再有已知产品实现缺口，剩余工作只并入 Release candidate：在冻结提交上重建四端、完成公开表面最终扫描并让远端 CI 复现，不能用当前 dirty 工作树的历史设备证据代替。

退出条件：新用户只看任一官方模板或公开文档都会得到同一种 `import infernux as inx` 写法；小写入口在大小写敏感的 Linux、Android 和 Web 包中真实存在；公共符号记录、存根与运行时一致；四端同项目复验通过；仓库公开表面不再推荐星号导入，且 CI 能阻止其回归。

## 9. 自动测试矩阵

### 9.1 Contract tests

- Python runtime catalog 的版本/平台/架构/hash 解析与目录键；
- Hub 同时安装 3.12/3.13、每个 Infernux 版本唯一绑定一个 Python ABI、缺失目标版本时在线版本可见但禁止安装、本地 wheel 立即拒绝、无隐式下载和无系统 Python fallback；
- 引擎 `Requires-Python`/wheel ABI 与已安装运行时的兼容性过滤；
- 项目 `pythonVersion` 持久化、`.runtime/pythonXY` 隔离、旧 3.12 项目探测和事务式 3.13 迁移回滚；
- 删除仍被引擎或项目引用的 Hub 运行时必须失败并列出引用者；
- target 注册/冲突/卸载；
- exporter API 版本与引擎范围；
- BuildPlan 确定性；
- 进度事件顺序、取消和异常传播；
- capability report 与 preflight；
- Host-only/native dependency 拒绝；
- package audit；
- 临时目录和失败回滚；
- 插件升级/降级；
- 同一 GUID 资产在四端 manifest 中身份一致；
- MCP operation schema、权限诊断、session 连续性、幂等性以及对统一 build service 的唯一调用路径；
- pointer/touch 跨帧状态、稳定 ID、phase/cancel/capture、坐标变换、compatibility mouse 去重、action mapping 和文本 composition contract。

### 9.2 平台 tests

- Windows：现有 Python/C++ 全量 suite、Editor/Player smoke；
- Linux：适用 Python/C++ 全量 suite、Headless、GUI、Player、文件大小写和动态库；
- Android：NDK compile、Gradle assemble、APK install、Activity lifecycle、Vulkan、多点触控/cancel、IME/keyboard inset、Back/旋转/safe area、音频、Python gameplay；
- Web：Emscripten compile、链接白名单、browser startup、WebGPU、Pointer Events/capture/cancel、DOM composition/VisualViewport/safe area、移动真机输入、音频、资源、Python gameplay、server headers；
- 平台专属 skip 必须带 capability 原因，不允许用大范围 skip 隐藏移植失败。

### 9.3 测试层级

- 本地每次提交：受影响 host unit/contract，CMake configure/build smoke；所有命令在 `conda activate infernux` 下运行；
- PR 快速门禁：Windows/Linux core + Headless + stage/package contract，输出 JUnit 和失败日志；
- PR 平台门禁：Android compile/emulator smoke + synthetic multi-touch/IME contract，Web compile/link/Chromium Pointer Events/composition smoke；
- Nightly：无缓存四端构建、Windows/Linux GUI smoke、模拟器生命周期/旋转/Back、桌面与移动浏览器矩阵、包审计和可复现性抽样；
- Release candidate：Windows/Linux 发布包、Android arm64 真机、Android Chrome 与 iPhone/iPad Safari 真机输入、Tier 1 浏览器/OS/GPU、包体/启动和长时间运行。

## 10. 性能与体积初始预算

预算用于发现回归，不代表永久产品承诺；冻结样例和设备后记录 p50/p95：

- Web：压缩传输体积不超过 100 MB；受控网络首次可玩不超过 30 秒；缓存加载不超过 5 秒；
- Android：不含模型的 stripped APK 基线不超过 80 MB；冻结中端机冷启动到接受输入不超过 8 秒；
- Desktop：重构后 Windows Player 启动和构建时间不得无解释显著退化；Linux 单独建立基线；
- 构建进度超过 500 ms 无更新必须由当前步骤/子进程状态解释，不能表现为无响应；
- 输入延迟分别记录 event acquisition → native snapshot → Python/action consumer → render response 的 p50/p95；高频 move/coalesced events 不得形成逐事件无界 Python 跨语言调用；
- 所有预算均报告未压缩、压缩、安装后和运行峰值内存，禁止只选最小数字。

## 11. 高风险项和止损规则

| 风险 | 早期证据 | 应对 | 禁止做法 |
|---|---|---|---|
| Python ABI 混装 | `python312`/`python313` 的 wheel、DLL/so 或 site-packages 出现在同一项目 | 版本化 catalog、项目绑定、安装前 ABI resolver、最终包审计 | 根据 PATH 猜解释器、缺失时自动选最近版本、原地覆盖旧 runtime |
| Hub 多版本状态失真 | UI 显示已安装但 marker/hash/架构不匹配，或删除仍被项目引用的版本 | 每版本独立 marker、启动 doctor、引用索引与受保护删除 | 仅以目录存在判断可用、删除 Hub runtime 时联动删除项目副本 |
| Web CPython 不可维护 | 静态模块、线程、包体或启动 spike 失败 | 按 Phase 6 Decision Gate 记录 A/B/C | 静默缩减 Python 语义 |
| CMake 重构与平台移植混杂 | Windows target/产物在拆分中漂移 | characterization 后按责任小步迁移，先结构后平台行为 | 一次提交同时拆文件、改 ABI 和加平台分支 |
| CI 矩阵成本失控 | 每个 PR 都重复全量 SDK 下载和四端 release | 快速/平台/nightly/RC 分层，Preset 共用，缓存不影响正确性 | 为节省时间永久取消平台门禁 |
| Windows 构建器难以抽离 | 同一职责在 UI/Nuitka/Host 重复 | characterization test 后逐段适配 | 一次性重写全部构建器 |
| Android 生命周期崩溃 | resume/surface recreate/低内存失败 | 生命周期状态机与设备回归 | 只测首次启动 |
| 第三方库不能交叉编译 | compile inventory 出现平台私有代码 | 上游选项、薄 adapter 或受控 fork | 在插件复制整套引擎源码 |
| WSLg 与真实 Linux 差异 | CI/发行版结果不一致 | WSLg 只作本地反馈，Ubuntu CI 为门禁 | 把 WSLg 当发布环境 |
| 模拟器 Vulkan 与真机差异 | x86_64 通过、arm64 失败 | 模拟器快测 + 真机 Release Gate | 用模拟器代替真机结论 |
| WebGPU 覆盖不足 | Tier 1 浏览器不可用 | 明确最低版本；基于数据评估 fallback | 假装所有浏览器支持 |
| 手机输入只在桌面模拟器通过 | 真机出现重复点击、粘住、IME 遮挡或坐标偏移 | Android/iOS 移动 Web 真机 Gate，多指/IME/旋转/后台矩阵 | 用鼠标点击或浏览器设备尺寸模拟代替触屏 |
| Touch 被鼠标兼容层掩盖 | 单指似乎可用，但多指和 cancel 丢失 | 独立 pointer contract、稳定 ID、compatibility event 去重 | 把 touch 永久映射成左键 |
| 插件接口过度设计 | hook 无第二消费者/无测试 | 只保留 Android/Web 共同需要的最小接口 | 为假想平台扩张核心 |
| 瘦身误删真实设备兼容性 | 主机测试通过但特定驱动/浏览器/生命周期失败 | 候选分类、外部约束登记、对应设备证据、一次收敛一个 owner | 按关键词批量删除、用新的静默 fallback 修补回归 |
| fallback 审计演化成永久重构 | 清单持续增长但核心路径和体积不收敛 | 冻结基线、按高乘数模块排序、每批量化收益与退出条件 | 为追求零 `try/except` 重写无关稳定代码 |

任何平台专属需求若需要改变核心 contract，先写 decision record 并由 Windows/Linux/Android/Web fixture 共同验证；不得直接在插件中复制第二套 Cook、依赖分析或包审计系统。

## 12. 0.4.0 强制验收清单

1. 根 CMake 已按 target ownership 和跨 target policy 拆分，Windows 重构前后的 target/产物/test characterization 一致；
2. Windows/Linux engine/Editor/Headless 有正式 configure/build/test/install/workflow Preset；四种 Player 平台插件分别拥有自己的 package/cross-build Preset 或模板；
3. 普通 configure/build 不写源码包目录，wheel/Player/Hub 从 `out/stage` 组装，最终发布物只进入 `dist/releases/<version>`；
4. Hub 可并列管理 `python312`/`python313`，以 3.13 为默认源；每个 Infernux 版本唯一绑定一个 Python ABI，用户创建项目时只选择引擎版本，Hub 自动派生项目 Python；安装 Infernux 版本时若缺失目标 Python，则明确禁止且不隐式下载或 fallback；Windows/Linux Hub 是分别由本机 runner 构建和验收的原生制品，各自只选择同平台 wheel、更新包和 manifest；官网始终提供两条独立下载路径；
5. 新项目明确绑定 Python 3.13；旧 3.12 项目不被自动改写，显式迁移可回滚；项目、Editor、Player 和 native payload 的 ABI 可由 package audit 证明未混装；
6. Windows/Linux GUI Editor 都能打开桌面 040 项目、编辑、保存和 Play；
7. Windows/Linux Headless 都能加载同项目、运行固定帧并输出结构化轨迹；
8. Windows/Linux wheel 的干净安装都只从自身 `python/Infernux/resources` 自动安装 MCP；四个平台目标在未安装插件时可见但不可构建，并明确引导用户从官方目录下载对应包；安装后才注册真实目标；
9. 安装 Android/Web 官方 InxPackage 后相应目标出现，禁用/卸载后干净消失；
10. 同一项目输出 Windows Player、Linux Player、APK、AAB 和 Web 目录；
11. 四端完成同一场景、UI、输入、音频、物理、资源和普通 Python gameplay 任务；
12. Android 的稳定多指 ID、move/up/cancel、UI capture、中文/emoji IME、软键盘 inset、Back、safe area、旋转和前后台恢复全部通过；
13. 移动 Web 的 Pointer Events/capture/cancel、compatibility mouse 去重、DOM composition、VisualViewport、safe area、地址栏/软键盘 resize 和 user activation 全部通过；
14. 同一最小 action mapping 能让键鼠/gamepad 和触控 UI 驱动同一 gameplay，不要求用户脚本把 touch 伪装成鼠标或键盘；
15. Android 模拟器、至少两档 arm64 真机通过；
16. Web 桌面 Tier 1 浏览器以及 Android Chrome、iPhone/iPad Safari 真机矩阵通过，部署 header 要求有 doctor；
17. Android/Web 产物不包含 Numba、Torch 或未声明 Host-only/native 模块；
18. 构建错误能定位到插件、脚本、依赖、ABI、SDK 或平台 capability；
19. 平台插件可以独立升级/回退并通过 contract suite；
20. Windows/Linux/Android/Web 产物和 evidence manifest 可从冻结环境重建；
21. PR、Nightly 和 Release CI 分层均使用仓库 Preset，不维护第二套 Actions 构建命令；
22. README/README-zh、官网、安装文档和 package metadata 与 evidence support matrix 一致；
23. 不生成、不宣传 Headless Player 包；
24. 官网和 README 只陈述已有公开证据，不把模拟器/compile spike 写成正式平台支持；
25. MCP 能稳定完成 040 项目的核心创建、编辑、Play、构建和诊断路径；各 Phase 暴露的阻塞性缺口均已修复并同步插件仓库、主仓 submodule 和官方包；
26. 0.4.0 Gate 关闭前不把 Torch/ModelRunner 混入默认构建路径。
27. Windows Editor 已通过 1920×1080@100% 基准、2K/4K 的 150%/200%/250% 缩放和混合 DPI 跨屏验收；字体/图标清晰，布局密度一致，Game/Scene framebuffer、输入命中、dock、弹窗及窗口恢复无缩放或坐标错误；
28. 真实的小写 `infernux` 公共入口、导出记录、类型存根、代码模板、插件模板、MCP 生成代码、README/官网/文档和桌面 040 四端项目均已统一为 `import infernux as inx`；公开表面不再推荐星号导入，CI 能阻止回归；
29. 官网提供独立 Windows/Linux Hub 下载路径；两套原生 Hub 均通过匿名官网 catalog 正确判断 latest、选择本机完整更新包并完成更新，未登录 GitHub或网络不可用时状态准确；版本通知来自官网 feed，社区页展示真实社区帖子；Hub/插件普通发布与上传不依赖整包 SHA-256；Hub、安装器、Splash、更新器与所有自绘控件在亮暗模式下统一使用语义主题令牌并通过视觉回归。
30. Windows/Linux/Android/Web 的最终 Player 不展开或直接暴露项目 `Assets`、`Library`、`Packages`、`ProjectSettings` 与松散 `BuildManifest.json`；项目内容只存在于当前版本的二进制内容包和资产目录中，包审计能阻止目录结构回归；此项不宣传为不可逆密码学加密。
31. 路径资产 API 在 Editor 中解析作者路径与 `.meta` GUID，在 Player 中只经冻结的 path→GUID→artifact binding 读取同一资产；需要真实文件路径的 exe/jar/wasm/pyd 等可从当前密封内容导出到产品私有缓存，缺失 binding 必须明确失败。
32. `Assets` 与 `Packages` 都生成并维护显式 `.meta`、参加同一次刷新与脚本候选扫描；安装、手动更新或删除插件后，组件/材质/Shader/任意文件在当前 Editor 会话中正确出现或消失，不依赖重启。
33. 官方 Git 插件只把小写 `package/` 打入 `.inxpkg`，仓库外层的 README、构建配置和独立 `package.py` 不入包；本地项目 `Packages/<name>` 仍可直接导出，缺少 manifest 时只按输出包名生成默认身份。

代码瘦身和全仓 fallback inventory 已从 040 强制验收移出，后续由独立 041 计划重新定义范围、基线和证据；040 只保留已发现且会阻塞当前四端主链的问题修复。

### 2026-09-03 构建缓存所有权收敛

构建缓存固定为三层：Hub Library 持久保存可跨项目复用的已下载插件、Release 包与源码快照；项目 `Cache/Plugins` 只承担插件安装事务暂存；项目 `Cache/Build/<target>` 保存该项目的 CMake、Gradle、Web 与 Nuitka 增量产物。SDK、解释器和平台工具链属于 Hub 管理的已安装组件，不伪装成缓存；Player 成品不得携带编辑器构建缓存。

桌面 Nuitka 默认全局目录已删除，普通项目构建使用 `Cache/Build/Desktop/{Staging,Nuitka,RuntimePacks,Requirements}`，Release 预编译使用 CMake build tree 内的显式缓存根。桌面 040 项目真实 Development Player 从当前源码在 **31.38 秒**内完成构建，缓存写入项目目录；旧 `C:\_InxBuild` 修改时间保持在构建前，成品 **1,162 文件、135,353,660 字节**且不含 `.inxrt/.inxmod/.inxpkg` 或 Cache 目录。对应桌面构建回归 **236 passed**。

Windows 中文项目路径不再迫使 Nuitka 缓存回到全局目录。编译期间只创建一个指向项目 `Cache/Build/Desktop` 的短生命周期 ASCII junction，Nuitka、SCons 与 MSVC 经该路径读写，结束后删除 junction，数据所有权仍完全属于项目。中文用户目录下的“无 MCP Editor → Development Player 构建 → MCP 外部验证启动 → 正常退出”真实链路在 **52.08 秒**内通过；MCP Supervisor 同时删除旧 PlayerLayout 分支，只接受当前 `direct_native_runtime`。

Android/Web 的固定原生依赖源码改由 Hub Library `Sources/<host>/<owner>/<name>/<revision>` 持有，项目 `Cache/Build` 只保留本项目的 CMake/Ninja/Gradle/Web 编译增量。zstd 身份从 annotated tag object 修正为相同 v1.5.6 源码对应的真实提交 `794ea1b0...`；Android 构建显式使用 Hub 中的 zstd、SPIRV-Cross 和 Volk，不再由每个项目的 CMake 隐式 FetchContent。当前三份浅 Git 工作树共约 **20.58 MiB**。040 arm64 Development APK 首次共享源构建在 **250.31 秒**内完成并安装到 HyperOS 真机；Activity 冷启动建立为 **0.64 秒**，随后达到 `ENGINE_LOADED`，LineRenderer、GPU 粒子、FBX 动画与运动传感器探针全部 Ready，fatal/Traceback 为 0。应用私有 `cache/player` 仅保留一个当前 `content-*` 代次；应用、ADB 与 Gradle daemon 已在取证后关闭。共享源与平台构建回归 **80 passed**。

## 13. 完成后的下一步

0.4.0 关闭后进入 0.4.2：在已验证的 Windows/Linux Host、Headless 和四端 Player 上设计 Torch authoring profile、`ModelAsset`、`ModelRunner`、provider contract、异步推理与训练→部署闭环。0.4.0 只为这些能力提供可靠平台，不预先决定具体推理 provider。
