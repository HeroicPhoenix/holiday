# Holiday API

基于 FastAPI 构建的节假日查询服务，支持 API 接口与可视化网页查询，并通过 APScheduler 每日自动更新节假日数据。数据来源为 GitHub 开源节假日 JSON 仓库，首次启动会自动下载并缓存至本地。

## 功能特性

- 日期节假日查询：通过 API 或网页查询某个日期是否为节假日
- 自动数据更新：每日凌晨 03:00 自动拉取最新节假日数据
- 本地数据缓存：首次运行自动下载所有年份的节假日数据，减少重复请求
- 前端可视化页面：使用 Jinja2 模板渲染，支持浏览器直接输入日期查询
- 健康检查接口：提供 `/health` 用于服务状态检测

## 技术栈

- FastAPI
- APScheduler
- Jinja2
- Pandas
- Requests

## 目录结构

```
holiday/
├── app.py                # 主应用入口
├── templates/
│   └── index.html         # 前端页面模板
├── data/                  # 节假日 JSON 数据缓存目录
└── requirements.txt       # 依赖列表
```

## 安装与运行

### 1. 克隆项目

```bash
git clone git@github.com:HeroicPhoenix/holiday.git
cd holiday
```

### 2. 安装依赖

建议使用 Python 虚拟环境：

```bash
pip install -r requirements.txt
```

### 3. 启动服务

```bash
uvicorn main:app --host 0.0.0.0 --port 12081 --reload
```

启动后访问：

- 前端页面：http://127.0.0.1:12081
- API 查询：http://127.0.0.1:12081/query?date=2025-10-01
- 健康检查：http://127.0.0.1:12081/health

## API 示例

### 请求

```
GET /query?date=2025-10-01
```

### 响应

```json
{
  "date": "2025-10-01",
  "is_holiday": true,
  "name": "国庆节",
  "type": "法定节假日"
}
```

## 定时更新

本服务使用 APScheduler 每日凌晨 3:00 自动更新节假日数据，如需修改定时任务时间，可调整 `main.py` 中的调度配置。

## 依赖

requirements.txt 内容：

```
fastapi
uvicorn
pandas
requests
apscheduler
jinja2
python-dateutil
pytz
```

## 授权许可

MIT License