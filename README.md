# escalador-publicador

Robô que publica no Instagram os posts agendados pelo **Escalador**.
Roda no GitHub Actions a cada 15 minutos, então funciona com o computador desligado.

- `queue.json`: fila de posts (escrita pelo site do Escalador)
- `accounts.json`: contas conectadas; os tokens ficam **criptografados** (a chave está só no secret `ESCALADOR_KEY` e no PC)
- Release `videos`: os vídeos agendados; cada um é apagado depois de publicado

Não edite à mão: use o site do Escalador.
