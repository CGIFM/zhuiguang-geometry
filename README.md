# 追光几何 · Zhuiguang Geometry

> 本地优先的文件夹版本记录工具：给任何目录拍快照，形成可回看的时间线，
> 随手对比、恢复、导出，并能在局域网两台机器之间或 NAS / WebDAV / S3 上同步。

**A local-first snapshot & version tool for ordinary folders** — drawings, documents,
design files, anything. Plain-Python standard library only, no dependencies,
no account, no cloud, no license key. Ships a web UI, a CLI, and a portable
build with an embedded Python runtime.

- 无激活码、无设备数限制、无云端依赖、无到期时间
- `.zgr/` 全部是明文件（zlib 压缩），任何工具都能检查
- 只依赖 Python 标准库（3.10+），不装第三方包
- 桌面客户端是独立窗口（WPF 宿主 + WebView2），双击就开，不经过浏览器
- 网页界面 + 命令行两套入口
- 支持 Windows 安装版 / **MSI** / 便携包（三者都自带 Python，不要管理员）

---

## 一、三分钟上手

### 方式 A：一键安装（推荐）

下载 `追光几何-<版本>-安装版.exe`，双击 → 下一步 → 完成。装完桌面就有图标。

- 装在当前用户目录，**不需要管理员、不弹 UAC**，公司电脑没权限也能装
- 自带 Python 运行时，目标电脑不需要装任何东西
- 在「设置 → 应用 → 已安装的应用」里能正常卸载
- 卸载只删程序，不动你的项目文件夹和 `%APPDATA%` 里的设置

安装选项里可以勾：桌面图标 / 开机自动启动 / 把 `zgr` 命令加进 PATH。

企业批量部署（组策略 / Intune / SCCM）用标准 MSI：`追光几何-<版本>.msi`，
支持 `msiexec /i 追光几何.msi /qn` 静默安装，卸载在「应用和功能」里。

自己从源码打安装包：

```bash
winget install JRSoftware.InnoSetup      # 一次性
python tools/build_installer.py --download
# 产物：dist/追光几何-<版本>-安装版.exe

# MSI（WiX 3.14 免安装包解压即用）
python tools/build_msi.py --wix C:\Tools\wix314
# 产物：dist/追光几何-<版本>.msi
```

### 方式 B：免安装便携包（U 盘、临时电脑）

1. 解压 `追光几何-<版本>-portable.zip` 到任意目录
2. 双击 **`追光几何.exe`** → 打开独立窗口（不是浏览器，没有地址栏）
3. 想常驻：双击 `安装.bat`（只写当前用户，脚本方式，不产生卸载项）

### 方式 C：源码

```bash
git clone https://github.com/CGIFM/zhuiguang-geometry.git
cd zhuiguang-geometry
python zgr.py serve --port 8795 --open
```

需要 Python 3.10+（3.13 已验证）。没有第三方依赖。

---

## 二、它到底在做什么

```
项目目录/                      ← 你的文件，原地不动
└── .zgr/
    ├── config.json           项目名、忽略规则、远端列表
    ├── objects/ab/abcdef…    内容寻址对象库（sha256，zlib 压缩）
    ├── refs/heads/main       分支指针
    ├── previews/             缩略图缓存
    └── backup/               被覆盖/删除前的文件
```

- 每个快照（commit）记录一棵树（tree）；树里每个文件指向一个按内容哈希命名的对象。
- 同样的文件内容只存一份；改一个文件只新增一个对象。
- 「远端有没有」只看对象号，所以同步天然幂等、可断点续传、可换任何存储。

### 三种同步方式

| 方式 | 适合 | 命令 |
|---|---|---|
| 直连另一台机器 | 两台都装了本软件，局域网 | `zgr sync <目录> --url http://对端:8795 --both --checkout` |
| 共享目录 | 已有 NAS 共享盘 / 映射盘 | `zgr sync <目录> --share \\nas\share\zgr --both` |
| **远端存储（新）** | WebDAV / S3 / MinIO / 任意对象存储 | `zgr remote add 名字 … && zgr sync <目录> --remote 名字` |

远端存储把对象直接摆到对方的目录里，布局和本地对象库一致：

```
<前缀>/<项目名>/objects/<oid 前两位>/<oid>
<前缀>/<项目名>/refs/<分支名>
```

### 配置远端存储

```bash
# NAS / SMB 共享目录（UNC 或映射盘）
zgr remote add 公司NAS --kind local --path "\\nas\备份\追光"

# WebDAV（群晖、威联通、Nextcloud、坚果云…），Basic / Digest 都支持
zgr remote add 坚果云 --kind webdav --url https://dav.jianguoyun.com/dav/追光 --user me@example.com --password ***

# S3 兼容（AWS / MinIO / 阿里云 OSS / B2）
zgr remote add 图床 --kind s3 --endpoint https://s3.us-east-1.amazonaws.com     --bucket zgr-backup --region us-east-1 --access-key AK --secret-key SK

zgr remote list                 # 看已配置的
zgr remote test 公司NAS         # 读写自检
zgr sync . --remote 公司NAS     # 双向同步
```

密码 / AK / SK **不会**写进项目目录，而是存在
`%APPDATA%\zhuiguang-record\secrets.json`（按仓库路径哈希 + 远端名索引），
项目目录整个拷给别人也不会带走凭据。

网页端在「同步」页有同样的面板，填完点「保存并测试」。

---

## 三、功能一览

- **时间线**：每次快照一行，可标颜色、打标签、写说明
- **对比**：文本逐行 diff；三维模型 / 图片 / DXF 有并排预览
- **恢复**：整个版本回到工作区，或只挑几个文件回来；覆盖前先备份
- **回收站**：`.zgr/backup/` 里整批复原，来回都能走
- **自动记录**：文件停止变化 N 秒后自动生成快照
- **一人一条线**：两个人各记各的分支，互相能看见对方的版本，合不合、合哪几个由你点
  「并入」决定（也可以切回「共用一条线」，让程序自动合并）
- **选择性合并**：两台机器都有新提交时，按文件挑「保留本机 / 采用对方 / 两边都留」
- **缩略图**：STEP/IGES/STL/DXF/DWG/SolidWorks 零件都能出图（详见下）
- **打包导出**：把某一版打成 zip 直接发人
- **体检与修复**：`zgr fsck` 查对象缺没缺、坏没坏（`--deep` 逐个重算校验和）；
  `zgr repair` 把另一台机器当备份，按对象号补齐缺的/坏的
- **命令行**：`init/snapshot/log/diff/restore/export/watch/sync/merge/remote/fsck/repair/…`

### 三维缩略图的三个来源

1. 文件里内嵌的预览图 / Windows 缩略图缓存（最快）
2. 浏览器里用 `occt-import-js` 解析 STEP/IGES、`STLLoader` 读 STL，生成网格并按 oid 回存
3. 服务端 `zgr/thumb3d.py` 用 z-buffer 光栅化渲染（纯 Python，无 OpenGL）

网格文件格式 `ZGM1`：`"ZGM1" | u32 顶点数 | u32 三角数 | f32 xyz×n | u32 i0,i1,i2×m`。

批量补图：

```bash
zgr warm-neutral . --minutes 30      # STEP/IGES/STL：无头浏览器解析
zgr warm-sw . --minutes 30           # SolidWorks 零件：本机装 SW 才能用
```

两者共用一把跨进程锁（`%TEMP%\zgr-sw-export.lock`），
所以网页上的临时请求不会被后台批量任务饿死。

### DWG 预览（可选）

DWG 需要 LibreDWG 的 `dwg2dxf`。它俩许可不同（LibreDWG 是 **GPL-3.0**），
所以**不随本仓库分发**：

```bash
python tools/fetch_libredwg.py --check   # 看看有没有
python tools/fetch_libredwg.py --zip <你下载的 libredwg 压缩包>
```

没有它，其它格式照常，只是 DWG 不能预览。

---

## 四、命令行速查

```bash
zgr init .                       # 把当前目录变成项目
zgr snapshot . -m "第一版"        # 拍一个快照
zgr log .                        # 时间线
zgr status .                     # 有哪些未记录的改动
zgr diff . <版本A> <版本B>        # 对比
zgr restore . <版本>              # 回到某个版本（先备份当前）
zgr export . <版本> ./导出目录     # 导出某一版
zgr pack . <版本>                 # 打成 zip
zgr watch . --interval 30        # 前台自动记录
zgr serve --port 8795 --open     # 网页服务
zgr backup . --list              # 回收站
zgr backup . --prune --keep 20   # 只留最新 20 批备份
zgr fsck .                       # 体检：对象缺没缺、坏没坏（只读）
zgr fsck . --deep                # 再逐个重算校验和，能查磁盘静默损坏
zgr repair . --url http://对端:8795  # 从另一台机器补齐缺的/坏的对象
zgr recover .                    # 收尾上次被断电打断的恢复
zgr live . --url http://对端:8795 # 前台实时同步（共用一条线，自动合并）
zgr live . --url http://对端:8795 --split  # 一人一条线：各记各的，只同步不合并
```

---

## 五、桌面壳（可选）

`desktop/` 是一个 WPF + WebView2 的薄壳，把网页界面装成独立窗口（不是浏览器标签）。
需要 .NET SDK；`desktop/build.ps1` 说明编译步骤。
不想要窗口的话，用浏览器打开 `http://127.0.0.1:8795/` 完全等价。

---

## 六、许可与第三方

本项目 MIT。第三方组件的许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

> 本项目借鉴了「时间线 + 颜色标记 + 快照恢复」这类交互理念，
> 代码为从零原创实现，不包含、不调用、不修改任何第三方程序或其二进制、资源、品牌。
