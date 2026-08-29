# Infernux Android 平台

这个官方 InxPackage 为 Infernux 添加 Android Player 构建目标。Android
工具链检查、构建计划、Host 模板、打包、产物审计以及模拟器/真机烟测均由
插件维护。最终 Player 只使用 Vulkan，不以 OpenGL 或 OpenGL ES 作为回退。

首批目标为：

- `android-x64-emulator`：用于 AOSP 模拟器上的快速本地验证；
- `android-arm64`：用于发布包和 arm64 真机。

插件要求 JDK 17、Gradle 8.12 与固定版本的 Android SDK/NDK。启动编辑器前
应配置 `JAVA_HOME`、`ANDROID_SDK_ROOT`，还可以配置
`INFERNUX_GRADLE_HOME` 与 `ANDROID_AVD_HOME`。目标发现和工具链诊断统一
通过 Infernux 构建服务完成。

Exporter 会按明确阶段逐步完成。目标已经可见并不代表不完整的构建会被伪装
成成功；doctor 或执行结果会直接指出当前缺少的前置条件。
