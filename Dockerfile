FROM public.ecr.aws/lambda/python:3.12

# Install dependencies
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy model + app
COPY app.py ${LAMBDA_TASK_ROOT}
COPY en_US-ljspeech-high.onnx ${LAMBDA_TASK_ROOT}
COPY en_US-ljspeech-high.onnx.json ${LAMBDA_TASK_ROOT}

CMD ["app.handler"]