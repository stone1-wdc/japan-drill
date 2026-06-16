[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/stone1-wdc/japan-drill)

# 日本語練習本 — Japanese Practice App

交互式日语学习应用，基于 Flask + SQLite，支持单词记忆、翻译练习、语音朗读、语法搜索。

---

## 项目结构

`
japan/
├── app.py                 # Flask 主应用
├── requirements.txt       # Python 依赖
├── Procfile               # Render 部署声明
├── .gitignore             # Git 忽略规则
├── templates/
│   └── index.html         # 前端单页 (Tailwind CSS CDN)
├── static/
│   ├── audio/             # 音频文件目录 (可选)
│   └── grammar/           # 语法 JSON 文件
│       ├── te-form.json
│       └── potential.json
└── book/
    └── chapters/          # 课本内容
        ├── chapter1.txt
        └── chapter6.txt
`

---

## 本地运行

`ash
pip install -r requirements.txt
python app.py
`

浏览器打开 http://localhost:5000，首次运行自动创建 japanese.db。

---

## 部署到 Render.com

### 1. 推送代码到 GitHub

将项目推送到 GitHub 公开仓库。

### 2. 创建 Web Service

1. Render 控制台 → New + → Web Service
2. 连接 GitHub 仓库
3. 填写配置：

| 配置项 | 值 |
|---|---|
| Name | japanese-practice |
| Runtime | Python 3 |
| Build Command | pip install -r requirements.txt |
| Start Command | python app.py |
| Instance Type | Free |

4. 点击 Deploy Web Service

### 3. 环境变量

无需手动设置。Render 自动注入 PORT，app.py 已适配。

### 4. SQLite 数据持久化

Render 免费实例使用临时文件系统，重启后磁盘文件会丢失。

已解决：app.py 在模块加载时自动执行 init_db()，每次启动都会建表。
用户进度在运行期间正常保存，重启后会归零，无需手动操作。

如需永久保存进度，可升级 Render 付费计划（持久磁盘）或替换为 Render PostgreSQL。

---

## API 路由

| 方法 | 路由 | 说明 |
|---|---|---|
| GET | / | 前端页面 |
| GET | /api/chapters | 列出所有章节 |
| GET | /api/chapter/<num> | 获取章节句子和单元 |
| GET | /api/progress?username=x | 获取最新学习进度 |
| POST | /api/progress | 更新学习进度 |
| GET | /api/audio?chapter=N&sentence_index=M | 获取音频文件 URL |
| GET | /api/grammar?keyword=xxx | 搜索语法条目 |

---

## 课本文件格式

`
## 第1週 1日目 单元名称
日语句子|读音——中文翻译
`

- ## 开头 = 单元标记
- | 分隔日文和读音
- —— 分隔日文和中文翻译

---

## 技术栈

- 后端：Flask + SQLite3（Python 内置）
- 前端：Tailwind CSS CDN + 原生 JavaScript
- 语音：Web Speech API（浏览器内置日语 TTS）