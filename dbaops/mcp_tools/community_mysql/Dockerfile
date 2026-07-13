FROM public.ecr.aws/lambda/nodejs:20-arm64
WORKDIR /var/task

COPY package.json package-lock.json* ./
RUN npm install --omit=dev

COPY handler.mjs ./

CMD ["handler.handler"]
