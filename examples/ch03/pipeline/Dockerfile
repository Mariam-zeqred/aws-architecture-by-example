# The training/scoring task image. Built and pushed by `cdk deploy` (the CDK
# bundles this directory as an asset); never built by `make check`.
FROM public.ecr.aws/docker/library/python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
ENTRYPOINT ["python", "-m", "cli"]
