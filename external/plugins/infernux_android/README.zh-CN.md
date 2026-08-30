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

## Release 签名

Release 构建默认生成 AAB。没有配置签名时，导出器仍会生成未签名 AAB，并在
构建结果中明确标记。准备发布到应用商店时，请在构建前设置：

- `INFERNUX_ANDROID_KEYSTORE`：keystore 路径；
- `INFERNUX_ANDROID_KEY_ALIAS`：签名密钥别名；
- `INFERNUX_ANDROID_KEYSTORE_PASSWORD`：keystore 密码；
- `INFERNUX_ANDROID_KEY_PASSWORD`：密钥密码；与 keystore 密码相同时可省略。

构建配置也可以声明 `android_keystore`、`android_key_alias`、
`android_keystore_password_env` 与 `android_key_password_env`。密码只从指定的
环境变量读取，不会写入生成的 Gradle 工程或构建清单。
