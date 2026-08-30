# Infernux Linux 平台

这个官方 InxPackage 提供 `linux-x64` Player 构建目标。Linux wheel 会自动
安装它；构建时使用 wheel 自带的原生 Player runtime、Vulkan 后端、Python
3.13 runtime pack 和共享构建服务。

插件负责 Linux 目标的发现与注册。卸载或禁用插件会移除 Linux 构建目标，
不改变平台无关的构建服务。
