# CS2 战术本

一个基于 Python 标准库、SQLite 和原生网页前端的 CS2 战术设计与管理工具。

## 本地运行

双击 `start.bat`，或在项目目录运行：

```powershell
python app.py
```

默认地址是 `http://127.0.0.1:8000`。如果 8000 被占用，本地会自动尝试后面的端口。

本地默认测试密钥是：

```text
local-dev-key
```

也可以自己指定：

```powershell
$env:APP_ACCESS_KEY="your-secret-key"
python app.py
```

## 数据库

默认数据库文件是项目根目录的 `tactics.db`，首次启动会自动创建并写入默认地图池：

- Mirage
- Dust2
- Ancient
- Nuke
- Overpass
- Anubis

可以通过环境变量指定数据库路径：

```powershell
$env:DB_PATH="C:\data\tactics.db"
python app.py
```

## Zeabur 部署

项目入口是 `app.py`，运行命令为：

```bash
python app.py
```

部署环境会通过 `PORT` 环境变量注入端口；应用检测到 `PORT` 后会监听 `0.0.0.0:$PORT`。

必须添加访问密钥环境变量：

```bash
APP_ACCESS_KEY=换成你自己的强密钥
```

如果需要持久保存 SQLite 数据，建议在 Zeabur 配置持久化存储卷，并把 `DB_PATH` 设置到卷路径，例如：

```bash
DB_PATH=/data/tactics.db
```

健康检查接口：

```text
GET /api/health
```

登录后可以在主界面下载结构化战术本：

```text
GET /api/export?format=docx
GET /api/export?format=pdf
```

导出内容按地图分组，每张地图包含 T 方战术、CT 方战术、T 方注意事项和技巧、CT 方注意事项和技巧。
