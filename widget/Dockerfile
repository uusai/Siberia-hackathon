FROM nginx:alpine

# Встраиваемый виджет отдаётся отдельным сервисом на своём порту, а не из
# фронтенда: его подключают с ЧУЖИХ сайтов, и раздача статики для внешних
# потребителей — это другой контур, чем интерфейс сотрудников вуза. Разные
# порты позволяют закрыть или вынести их по отдельности, не трогая соседа.
COPY widget.js  /usr/share/nginx/html/widget.js
COPY index.html /usr/share/nginx/html/index.html
COPY app.js     /usr/share/nginx/html/app.js
COPY widget.css /usr/share/nginx/html/widget.css
COPY demo.html  /usr/share/nginx/html/demo.html
COPY nginx.conf /etc/nginx/conf.d/default.conf

RUN chmod -R a+rX /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
