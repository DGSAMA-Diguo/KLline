# A 股多指标片段匹配工具

> **仓库架构（主次关系）**
>
> - **GitHub = 官方主源（代码主权）**：`DGSAMA-Diguo/KLline`（main 分支）
>   所有 Release、代码提交、更新分片 update.json 的权威记录均在 GitHub 上。
> - **Gitee = 国内镜像源（CDN 加速）**：`dgsproject/kline`（master / mobile / pc 三分支）
>   为国内用户提供更快的下载入口，内容与 GitHub 主源保持一致同步。
>
> | 分支 | 内容 | GitHub 官方主源（权威记录） | Gitee 国内镜像源（推荐国内用户下载） |
> |------|------|------------------------|-------------------|
> | **main / master** | 源代码 | [源码 ZIP - GitHub](https://github.com/DGSAMA-Diguo/KLline/archive/refs/heads/main.zip) | [源码 ZIP](https://gitee.com/dgsproject/kline/releases/download/v1.2.8/KLineSource.zip) |
> | **mobile** | 手机版 APK（含 2 年缓存行情） | [KLineMobile v1.5.5 - GitHub](https://github.com/DGSAMA-Diguo/KLline/releases/download/1.5.5/KLineMobile-v1.5.5.apk) <br> 兼容鸿蒙/Honor/安卓 8.0+/iOS | [KLineMobile v1.5.5 - Gitee 国内镜像](https://gitee.com/dgsproject/kline/releases/download/1.5.5/KLineMobile-v1.5.5.apk) <br> 【收藏夹长按菜单 + 多选批量管理 + 卡片化界面】国内网络直接下载即可 |
> | **pc** | 电脑版便携版（免安装） | [PC便携版 - GitHub](https://github.com/DGSAMA-Diguo/KLline/releases/download/v1.2.8/KLineAgent-portable.zip) <br> Windows 10+ 解压后双击 KLineAgent.exe 即可运行 | [KLineAgent-portable v1.2.8.zip](https://gitee.com/dgsproject/kline/releases/download/v1.2.8/KLineAgent-portable.zip) |
>
> - 手机版请切换到 **mobile** 分支
> - 电脑版请切换到 **pc** 分支
> - 源代码：GitHub 主仓在 main 分支；Gitee 镜像在 master 分支
>
> ## 荣耀 / 鸿蒙手机安装提示
>
> 若您是荣耀 MagicOS 6 / 7 / 8 或 HarmonyOS 用户，且仍提示「解析包时出现问题」，
> 请按以下步骤处理（v1.5.5 已内置荣耀专项兼容修复，99% 机型可直接安装）：
>
> 1. 卸载手机上已安装的 v1.2.x 旧版（签名变更，必须先卸旧版）
> 2. 下载 v1.5.5 APK 时**不要用微信/QQ 内置浏览器打开**（它们自带二次校验会误判），
>    请使用手机自带的「浏览器」APP 打开本 README 再点击下载
> 3. 安装时如荣耀弹出「纯净模式已保护您的手机」：
>    → 设置 → 安全 → 更多安全设置 → 关闭「纯净模式」/ 选择「仍要安装」
> 4. 以上 3 步全部做完仍失败，请重启手机再试一次（部分荣耀机型 PackageManager
>    有 APK 哈希缓存，重启后才会刷新）。
>
> ## 手机版更新说明
>
> 手机版自 **v1.3.7+** 起启用「8 通道自动切换」更新机制，并内置荣耀专项兼容修复；
> **v1.3.8** 新增「自动监控预警」功能（测试版）；
> **v1.3.9** 对内置行情数据做 gzip 压缩优化（APK 从 48MB 缩到 41MB，分片从 7 个减到 6 个）；
> **v1.4.0** 把应用内更新从「分片串行下载」改造为「3 线程并发下载」（下载耗时减半）；
> **v1.4.1** 把预警功能从「单股监控」改造为「全市场扫描」；
> **v1.4.2** 修复两项用户反馈的更新体验问题；
> **v1.4.3** 按用户反馈改进放量预警口径；
> **v1.5.0** 按用户反馈重建 UI + 修复实时行情无法更新 + 平板适配；
> **v1.5.1** 修复「行情显示和实际不一样」两项根因 Bug（f124 日期解析 + f5 成交量单位）；
> **v1.5.2** 收藏 Toast 浮层提示 + 按钮三态交互增强 + 时间轴布局/手势优化 + 底部菜单再缩小 1/3；
> **v1.5.3** 修复 K 线断层（腾讯历史日 K 自动补全缺口）+ 实时行情仅开市时段刷新 + 500 根滑动窗口与 LRU 内存控制；
> **v1.5.4** 修复 APK 内 WebView 白名单拦截腾讯接口导致补数失效的问题（白名单扩至 4 域名）；
> **v1.5.5** 收藏夹大升级：
>
> - 长按任意收藏弹出操作菜单（多选 / 置顶 / 重命名 / 删除），ActionSheet 底部滑出
> - 多选模式：点击勾选、全选/取消全选、批量删除、完成退出
> - 收藏条目卡片化：迷你走势缩略图（红涨绿跌）+ 代码徽标 + 日期区间 + 紧凑时间
> - 收藏夹本地存储上限 5MB → 15MB（约可存 300 条 400 根大收藏）
> - APP 内重命名弹窗渲染为原生输入框（MainActivity 新增 onJsPrompt 回调）
>
> ## 手机版核心功能
>
> - 全市场 A 股相似 K 线片段匹配（K 线 / 成交量 / MACD 三指标可多选）
> - 内置约 2 年全市场日 K 缓存，断网可用；联网自动补全缺口并显示最新行情
> - 5 分钟自动刷新实时行情（仅开市时段），非开市时段显示「休市中」
> - 全市场扫描预警（涨跌幅 / 放量倍数 / 形态相似度），3 分钟一轮
> - K 线收藏夹：框选片段保存、缩略图预览、相似匹配
>
> ## 电脑版（v1.2.8，未随手机版更新）
>
> - Windows 10+ 免安装便携版；数据与手机版同源
> - 如需电脑版新功能请单独反馈，将另行发布

（README 由发布脚本自动生成）
