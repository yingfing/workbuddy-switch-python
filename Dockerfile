FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY wbspy ./wbspy
RUN pip install --no-cache-dir .

# 凭据 / 账号池 / 配置全部落在这个目录，挂卷即持久化
VOLUME ["/root/.wbspy"]
EXPOSE 8178

CMD ["wbspy", "web", "--host", "0.0.0.0", "--port", "8178"]
