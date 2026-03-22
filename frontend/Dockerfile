FROM node:20-alpine

WORKDIR /app

ENV PATH=/app/node_modules/.bin:$PATH
ENV npm_config_update_notifier=false
ENV npm_config_fund=false

COPY package.json package-lock.json* ./

RUN npm install

COPY . /app

EXPOSE 5173

CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]

