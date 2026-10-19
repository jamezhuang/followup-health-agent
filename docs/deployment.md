# 部署复诊健康咨询agent

部署结构：浏览器 → Nginx → Next.js / FastAPI。API使用本地SQLite及持久化LangGraph检查点，单进程后台队列。仓库托管不等于服务上线；GitHub Pages不能直接运行该后端。

## 本机容器启动

复制根目录 .env.example 为 .env，执行 `docker compose up -d --build --wait`。

默认仅监听127.0.0.1:8080，访问 /workspace、/api/health、/api/docs。浏览器通过同源 /api 转发，避免把客户端固定指向开发电脑的8002端口。内网服务api:8000、web:3000不单独映射宿主端口。

demo模式无需密钥。账号通过页面注册；管理员通过 `docker compose exec api python -m app.account_admin admin001 --role admin --generate-password` 创建。生成的密码不写入GitHub。

live模式配置根 .env 的供应商密钥，设置PROVIDER_MODE=live，执行 `docker compose up -d --force-recreate api`。外部搜索、OCR、ASR和Crawl4AI默认关闭，需额外配置才能使用，容器基础镜像未安装可选浏览器读取依赖。

## 持久化与备份

api_data卷挂载/app/.cache，保存账号、报告、业务数据及检查点。停止容器 `docker compose down` 保留卷。不要使用带-v的删除命令，除非明确要清空所有运行数据。备份需同时包含业务库、账号库、检查点库及相关上传数据；活跃SQLite库应使用一致性备份方式。

数据库不会上传到GitHub或打入镜像。重新拉取仓库启动的是空工作台，不包含本机已发布知识库和患者数据；公共知识通过管理员上传、核对及发布。

## 外网部署

需要Linux服务器或可运行Compose的主机，配置真实域名与HTTPS反向代理。当前Compose的宿主端口仅绑定本机，宿主HTTPS代理可转发到127.0.0.1:8080。更新CORS_ORIGINS为真实HTTPS域名并启用COOKIE_SECURE=true。HTTPS终止层应正确设置转发协议；证书和供应商凭据由部署环境保存。

API仍限制单worker。部署为多个实例前，必须替换或扩展队列、共享存储、幂等与互斥机制，并完成跨实例故障测试。本项目当前没有宣称已实现Redis缓存、MQ投递或MySQL分布式事务。

## 验证

`docker compose config --quiet`校验配置；`docker compose ps`检查健康；访问/workspace和/api/health验证入口。Actions包含API测试、Web构建、Compose构建和启动检查，不自动部署生产服务器。

本机容器与CI构建结果需要以实际执行记录为准；仓库创建、代码推送、CI通过和公网服务可用是四个不同状态。
