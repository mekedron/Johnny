# Frontend image: SvelteKit dev server. Production build comes later;
# for now we run `vite dev` to match the local development experience.

FROM node:20-alpine

RUN npm install -g pnpm@9

WORKDIR /workspace

COPY package.json pnpm-lock.yaml .npmrc ./
RUN pnpm install --frozen-lockfile

COPY . .

EXPOSE 5173

CMD ["pnpm", "exec", "vite", "dev", "--host", "0.0.0.0", "--port", "5173"]
