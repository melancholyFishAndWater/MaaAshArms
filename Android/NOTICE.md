# Android 分发与源码

MaaAshArms 的任务资源与原有 agent 源码继续按仓库根 LICENSE 的 MIT 许可提供。
Android 客户端使用 Aliothmoon/MaaFwApp，遵循 GNU AGPL v3.0；其许可证随 APK 内资源以 LICENSE-MaaFwApp 提供。组合分发须遵守各组成部分的许可及 AGPL 对应源码提供义务，本说明不将 MaaFwApp 重新许可为 MIT。

- 本项目及对应构建配置：https://github.com/melancholyFishAndWater/MaaAshArms
- Android GUI 源码：https://github.com/Aliothmoon/MaaFwApp
- Android Python 内核源码：https://github.com/Aliothmoon/MaaAgentCoreAndroid
- MaaFramework 源码：https://github.com/MaaXYZ/MaaFramework

获取某个发布 APK 的对应源码时，应检出该 APK 对应的 MaaAshArms tag，并执行 `git submodule update --init --recursive`。Android/MaaFwApp 的 gitlink 固定 GUI 源码提交，Android 构建日志记录配对的 MaaFramework 与 Python 内核 tag。分发自行修改的 APK 时，也须提供该修改版本的对应源码与构建资料，不能仅链接未修改的上游。

运行要求：灰烬战线 B 服、Android 10 或以上、Shizuku 或 root。首版仅提供 1280×720 后台虚拟屏幕模式。APK 构建通过不等于设备运行验收通过。
