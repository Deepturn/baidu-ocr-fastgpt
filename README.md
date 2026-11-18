# 百度 OCR 适配器 for FastGPT

此服务接收 PDF、JPEG 或 PNG，将识别结果按页拼接为纯文本 Markdown。PDF 先在本地逐页渲染，页面图片再通过 HTTPS 发送到百度云高精度 OCR。运行环境需能访问百度云。

## 构建与启动

需要 Docker Engine、Buildx 和 Docker Compose。镜像平台为 `linux/amd64`。

```bash
docker buildx build --platform linux/amd64 --load -t fastgpt/baidu-ocr-adapter:0.1.0 .
```

在部署目录创建本地 `.env`，设置以下变量，也可以由部署环境直接注入。

| 变量 | 用途 |
| --- | --- |
| `BAIDU_API_KEY`、`BAIDU_SECRET_KEY` | 百度云 OCR 应用凭证 |
| `FASTGPT_OCR_TOKEN` | FastGPT 调用适配器的独立 Bearer Token，至少 32 字符 |
| `BAIDU_AUTH_URL`、`BAIDU_OCR_URL` | 上游接口地址，默认使用百度云官方地址 |
| `OCR_BIND_ADDRESS`、`OCR_HOST_PORT` | 宿主机监听地址和端口，默认 `127.0.0.1:7231` |

```bash
docker compose config --quiet
docker compose up -d --pull never
curl -fsS http://127.0.0.1:7231/healthz
```

已有 Docker 的离线安装目标可通过 `docker save` 导出镜像，再在目标机用 `docker load` 导入。Compose 使用同一个镜像名称和标签。

## FastGPT 接入

将自定义 PDF 解析地址配置为 `http://<FastGPT 可达的地址>:7231/v2/parse/file`，解析服务 Key 填入 `FASTGPT_OCR_TOKEN`。FastGPT 在另一个容器或主机时，应使用受控内网地址，或在共享 Docker 网络中使用适配器的服务名和容器端口 `8000`。图片是否走该接口取决于 FastGPT 版本和上传入口。

`POST /v2/parse/file` 接收 `Authorization: Bearer <token>` 和 multipart 的 `file` 字段，返回 `success`、`markdown`、`pages`、`error`。默认单文件最大 20 MiB、PDF 最大 100 页，总时限为 540 秒。任一页解析失败时整份请求失败。

## 测试

使用 Python 3.12。在 `service/` 下执行以下命令。测试使用模拟上游和合成文件。

```bash
python -m pip install -r requirements-dev.txt
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q
```
