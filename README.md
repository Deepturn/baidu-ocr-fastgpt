# 百度 OCR 适配器 for FastGPT

这个适配器可以作为 FastGPT 的自定义 PDF 增强解析服务。它接收 PDF、JPEG 或 PNG，按页返回纯文本 Markdown。PDF 先在本地逐页渲染，再把页面图片发往百度云高精度 OCR。运行环境需要能通过 HTTPS 访问百度云。

## Docker 部署

准备 Docker Engine、Buildx、Docker Compose，以及已开通百度云高精度 OCR 的应用凭证。当前 Dockerfile 构建 `linux/amd64` 镜像。

### 1. 克隆并构建

```bash
git clone https://github.com/Deepturn/baidu-ocr-fastgpt.git
cd baidu-ocr-fastgpt
docker buildx build --platform linux/amd64 --provenance=false --sbom=false \
  --load -t fastgpt/baidu-ocr-adapter:0.1.0 .
```

### 2. 配置凭证

用 `openssl rand -hex 32` 生成一份适配器访问密钥。将以下示例写入部署目录的本地 `.env`，并替换三个 `replace-with-` 占位值。

```dotenv
BAIDU_API_KEY=replace-with-baidu-api-key
BAIDU_SECRET_KEY=replace-with-baidu-secret-key
FASTGPT_OCR_TOKEN=replace-with-generated-hex-token
OCR_BIND_ADDRESS=127.0.0.1
OCR_HOST_PORT=7231
```

`BAIDU_API_KEY` 和 `BAIDU_SECRET_KEY` 用于访问百度云，`FASTGPT_OCR_TOKEN` 用于 FastGPT 调用适配器。保管好 `.env` 并执行 `chmod 600 .env`。Compose 已预设百度官方接口地址，自定义上游时可设置 `BAIDU_AUTH_URL` 和 `BAIDU_OCR_URL`。

### 3. 启动并检查

```bash
docker compose config --quiet
docker compose up -d --pull never --wait
docker compose ps
curl -fsS http://127.0.0.1:7231/healthz
```

健康检查应返回 `{"status":"ok"}`。启动失败时运行 `docker compose logs --tail=100 baidu-ocr-adapter`。要验证百度云调用，在当前目录放一份可发送给百度云的 `sample.pdf`，然后在 Bash 中运行以下命令，按提示输入 `FASTGPT_OCR_TOKEN`。

```bash
IFS= read -r -s -p 'FASTGPT_OCR_TOKEN: ' ocr_test_token
printf '\n'
printf 'header = "Authorization: Bearer %s"\n' "$ocr_test_token" | \
  curl --fail-with-body --silent --show-error --config - \
    --form 'file=@sample.pdf' \
    http://127.0.0.1:7231/v2/parse/file
unset ocr_test_token
```

成功时响应包含 `"success": true`、`markdown` 和 `pages`。即使 HTTP 状态为 200，也请检查 `success` 和 `error`。

### 离线导入镜像

在构建机导出镜像，将 tar 文件和 `docker-compose.yml` 一并传到目标机。在目标机按上面的示例配置本地 `.env`。

```bash
# 构建机
docker image save -o baidu-ocr-adapter-amd64.tar fastgpt/baidu-ocr-adapter:0.1.0

# 目标机
docker image load -i baidu-ocr-adapter-amd64.tar
docker compose up -d --pull never --wait
```

目标机加载与启动镜像时可离线。执行识别时仍需访问百度云。

## FastGPT 接入

### 1. 确定解析地址

| 部署方式 | FastGPT 中填写的完整 URL |
| --- | --- |
| 两个 Compose 项目在同一台 Docker 主机 | `http://baidu-ocr-adapter:8000/v2/parse/file`，按下面的步骤接入共享网络 |
| FastGPT 通过内网访问适配器宿主机 | `http://10.0.0.12:7231/v2/parse/file`，将示例 IP 改为适配器宿主机内网 IP，将适配器的 `OCR_BIND_ADDRESS` 设置为该 IP 后重新运行 `docker compose up -d --pull never --wait` |
| FastGPT 直接运行在同一宿主机上 | `http://127.0.0.1:7231/v2/parse/file` |

FastGPT 在容器内时，`127.0.0.1` 指向 FastGPT 容器自身。通过内网端口访问时，将端口的访问来源限制为 FastGPT 服务端。

**同机 Docker 共享网络。** 在宿主机创建网络，再将下面的配置分别合并进两个项目现有的 Compose 文件。保留已有服务配置和已有网络。若某服务原来未声明网络，列表中保留一个 `default`。

```bash
docker network inspect fastgpt-ocr >/dev/null 2>&1 || docker network create fastgpt-ocr
```

适配器 Compose 配置片段如下。

```yaml
services:
  baidu-ocr-adapter:
    networks:
      - default
      - ocr-shared
networks:
  ocr-shared:
    external: true
    name: fastgpt-ocr
```

FastGPT Compose 配置片段如下。按实际服务名调整 `fastgpt-app`，并把 `ocr-shared` 追加到它的原有网络列表。

```yaml
services:
  fastgpt-app:
    networks:
      - ocr-shared  # 追加到原有网络列表
networks:
  ocr-shared:
    external: true
    name: fastgpt-ocr
```

在适配器目录重新运行 `docker compose up -d --pull never --wait`。FastGPT 的网络设置会在下一步应用 Compose 配置时生效。

### 2. 按 FastGPT 版本填写配置

以下示例使用同机共享网络。其他部署方式用上表对应的完整 URL。

**使用环境变量的开源版，例如 4.15.x 至 4.17.x。** 将以下字段合并到 FastGPT 的 Compose 服务配置。[FastGPT 环境变量说明](https://doc.fastgpt.cn/zh-CN/self-host/config/env#pdf-增强解析配置)

```yaml
services:
  fastgpt-app:
    environment:
      CUSTOM_PDF_PARSE_URL: "http://baidu-ocr-adapter:8000/v2/parse/file"
      CUSTOM_PDF_PARSE_KEY: "${FASTGPT_OCR_TOKEN:?Set FASTGPT_OCR_TOKEN in the FastGPT deployment environment}"
```

在 FastGPT 部署目录的 `.env` 中增加下列变量，并使用适配器 `.env` 中的同一个值。

```dotenv
FASTGPT_OCR_TOKEN=replace-with-the-same-generated-hex-token
```

**仍读取 `config.json` 的旧版开源部署。** 将下面的对象合并到现有 `config.json` 中，保留其他字段，将 `<FASTGPT_OCR_TOKEN>` 替换成适配器的实际密钥，随后重启 FastGPT 应用。开源版从 4.15.0 起将这两项迁移到环境变量。[FastGPT 4.15.0 升级说明](https://doc.fastgpt.cn/zh-CN/self-host/upgrading/4-15/41500)

```json
{
  "systemEnv": {
    "customPdfParse": {
      "url": "http://baidu-ocr-adapter:8000/v2/parse/file",
      "key": "<FASTGPT_OCR_TOKEN>",
      "price": 0
    }
  }
}
```

**商业版或通过后台管理解析服务的版本。** 在「管理员 → 系统配置 → 基础配置 → PDF 解析配置」中填写自定义 PDF 解析地址和密钥，然后保存。界面提供解析服务选择器时，选择「自定义 PDF 解析」。地址填上表对应的 URL，密钥填适配器的 `FASTGPT_OCR_TOKEN`。[FastGPT 自定义 PDF 解析接入说明](https://doc.fastgpt.cn/zh-CN/self-host/custom-models/oxr#3-接入-fastgpt)

修改了 FastGPT Compose 的网络或环境变量时，在 FastGPT 部署目录执行以下命令。服务名以实际部署为准。

```bash
docker compose config --quiet
docker compose up -d --no-deps fastgpt-app
```

### 3. 在 FastGPT 中试用

在知识库导入一份可发送给百度云的扫描 PDF，勾选「PDF 增强解析」，导入后检查分块中是否出现扫描页上的文字。应用对话需要在应用的文件上传配置中单独开启「PDF 增强解析」。JPEG 和 PNG 可以直接请求适配器接口，FastGPT 的图片上传是否走该接口取决于版本和入口。[FastGPT 文件输入说明](https://doc.fastgpt.cn/zh-CN/guide/build/general/fileInput)

## 接口与限制

`POST /v2/parse/file` 接收 `Authorization: Bearer <token>` 和 multipart 的 `file` 字段，返回 `success`、`markdown`、`pages`、`error`。默认单文件最大 20 MiB、PDF 最大 100 页，总时限为 540 秒。任一页解析失败时整份请求失败。

## 测试

使用 Python 3.12。在 `service/` 下执行以下命令。测试使用模拟上游和合成文件。

```bash
python -m pip install -r requirements-dev.txt
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q
```
