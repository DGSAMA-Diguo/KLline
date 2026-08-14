移动端源码补丁
===============

本文件夹包含修改后的移动端源码 app.js，用于替代 mobile/app.js 原文件。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

一、变更内容

本次修改共 4 项功能，仅涉及 app.js 一个文件：

1. 缩短初始显示范围
   - 新增常量 DEFAULT_VIEW_BARS = 120（约半年交易日）
   - 加载股票时主图初始只显示最近 120 根 K 线，不再一次性显示全部 700 根
   - 详情窗口同样初始只显示最近 120 根
   - 用户仍可点击"显示全部"按钮查看完整历史

2. 增加日期轴标签数量
   - 手机端从 3 个日期标签增至 5 个
   - 宽屏从 5 个增至 7 个
   - 时间轴上日期更密集，更容易辨认时间范围

3. 滑块精度模式（核心新功能）
   - 按住滑块横向拖动后，手指向上抬起越高，滑块移动幅度越小
   - 灵敏度公式：1 / (1 + 上抬像素 / 50)
   - 上抬 0px → 灵敏度 1.0（正常速度）
   - 上抬 50px → 灵敏度 0.5（移动幅度减半）
   - 上抬 100px → 灵敏度 0.33（约为正常的三分之一）
   - 上抬 150px → 灵敏度 0.25（约为正常的四分之一）
   - 适用于全部 4 个滑块：主图起点、主图终点、详情起点、详情终点

4. 防误触逻辑保持不变
   - 手指先纵向滑动 → 仍恢复滑块原值（与原来一致）
   - 纵向判定为页面滚动，不触发精度模式
   - 只有判定为横向拖动后，上抬才触发精度调节


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

二、使用步骤

前提条件：
- 电脑上已安装 Python 3 和 Git
- 电脑上已有桌面版行情缓存 data/kline_cache.db（运行 agent.py 生成）
- 电脑上已安装 Android SDK 和打包工具（Android Studio / Gradle）
- 电脑上已有签名证书 KLineMobile-release.jks

操作步骤：

1. 下载本文件夹
   - 从 Gitee 仓库下载 mobile/update-patch/ 整个文件夹

2. 替换源码
   - 将 update-patch/app.js 复制到仓库根目录的 mobile/app.js
   - 命令示例（在仓库根目录执行）：
     cp mobile/update-patch/app.js mobile/app.js

3. 重新生成移动端 HTML
   - 在仓库根目录执行：
     python mobile/build_mobile.py
   - 这会读取 data/kline_cache.db，生成 mobile/KLineMobile.html
   - 确认输出显示证券数量、K 线数量和数据日期

4. 将 HTML 放入 Android 工程
   - 将生成的 KLineMobile.html 复制到：
     mobile/android-app/assets/KLineMobile.html
   - 命令示例：
     cp mobile/KLineMobile.html mobile/android-app/assets/KLineMobile.html
   - 也可以运行 mobile/android-app/prepare_apk_asset.ps1 自动完成

5. 递增版本号
   - 打开 mobile/android-app/build.gradle（如果有）
   - 将 versionCode 加 1（例如从 9 改为 10）
   - 修改 versionName 为新的版本名称

6. 打包 APK
   - 在 Android Studio 中打开 mobile/android-app/
   - 执行 Build > Generate Signed APK
   - 使用 KLineMobile-release.jks 签名
   - 生成 KLineMobile.apk

7. 生成更新分片
   - APK 生成后，按以下方式分片（每片不超过 9MB）：
     split -b 9m KLineMobile.apk KLineMobile.apk.part
   - 将分片重命名为 KLineMobile.apk.part001、part002、...

8. 计算 SHA256
   - 计算完整 APK 的 SHA256：
     sha256sum KLineMobile.apk
   - 计算每个分片的 SHA256

9. 生成 update.json
   - 参考 mobile/gitee-update/发布说明.txt 的格式
   - 填写 version_code、version_name、sha256、size、parts 数组

10. 上传到 Gitee
    - 确保仓库为 master 分支
    - 先上传全部分片到 update/ 目录
    - 最后上传 update.json
    - 删除旧版本不再引用的分片


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

三、文件清单

本文件夹包含：
  app.js          — 修改后的移动端源码（直接替换 mobile/app.js）
  补丁说明.txt     — 本文件


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

四、注意事项

1. 只需替换 app.js，styles.css 和 index.template.html 未修改
2. 替换后必须重新执行 build_mobile.py 生成新的 HTML
3. 新 HTML 必须重新打包进 APK，不能单独推送到手机
4. 手机端 WebView 不允许加载外部 JS，无补丁热更新能力
5. 签名证书和密码不得上传到公开仓库
