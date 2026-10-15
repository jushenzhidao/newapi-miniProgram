FROM python:3.12-slim

# mysqlclient 替代 pymysql 需要 gcc；用 pymysql 纯 Python 驱动则不需要。
# 本镜像同时装 curl 供健康检查。
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# uv：快速安装依赖（ghcr.io 在本机网络不稳定，改从 PyPI 装 uv 二进制）
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple uv

ENV UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /srv

# 先装依赖（层缓存）：只拷依赖清单
COPY pyproject.toml ./
RUN uv pip install --system -e . || uv pip install --system .

# 再拷代码
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

EXPOSE 8000

# 默认命令由 docker-compose 覆盖；容器单跑时用 API
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
