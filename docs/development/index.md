# 本地开发

```bash
uv sync --all-groups
make check
make docs-serve
```

文档开发服务器默认位于 `http://127.0.0.1:8000`。生产路径 `/mkdocs/docs` 由 Manager 挂载构建后的
`site` 目录，本地预览的根路径不同，但页面和相对链接一致。

启动完整依赖：

```bash
cp .env.dev.example .env
docker compose up -d --build
```

不要在测试中使用真实 provider key 或生产 token。
