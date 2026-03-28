# IFSP Normas — Sistema de Publicação de Normativos

Portal web institucional do **Instituto Federal de Educação, Ciência e Tecnologia de São Paulo** para gestão do ciclo de vida de normativos institucionais: portarias, resoluções, instruções normativas, editais, deliberações e documentos similares.

🔗 **Acesso:** https://auto.normas.ifsp.edu.br

---

## O que o sistema faz

1. Um **servidor** envia o PDF do normativo pelo portal
2. O sistema extrai o texto automaticamente e uma IA gera um FAQ sobre o documento
3. Um rascunho estruturado é criado no [normas.ifsp.edu.br](https://normas.ifsp.edu.br) (Bookstack) com três seções: Perguntas Frequentes, Texto Completo e link de download
4. Um **revisor** ou **administrador** analisa o rascunho e o publica na prateleira correta, ou o descarta
5. Normativos publicados podem ser **revogados**: a IA gera um resumo estruturado e o documento é movido para a prateleira de Revogadas
6. Todas as ações ficam registradas no log de auditoria

---

## Papéis de usuário

| Papel | Quem é | O que pode fazer |
|-------|--------|-----------------|
| **Servidor** | Servidores em geral | Enviar normativos via upload |
| **Revisor** | Responsáveis pela publicação | Enviar + revisar, publicar e revogar normativos |
| **Administrador** | Gestores do sistema | Tudo acima + excluir registros, gerenciar usuários e ver log |

O acesso é restrito a contas `@ifsp.edu.br` via Google Workspace. O papel padrão no primeiro login é **Servidor** — um administrador deve promover o usuário se necessário.

---

## Manual de Operação

### Para todos os usuários

**Acesso:**
1. Acesse https://auto.normas.ifsp.edu.br
2. Clique em **Entrar com Google Workspace**
3. Selecione sua conta `@ifsp.edu.br`
   > Se o Google selecionar automaticamente uma conta Gmail pessoal, clique em "Sair do Google" no link indicado na tela de login e tente novamente com sua conta institucional

---

### Papel: Servidor — Enviar um normativo

1. Na tela inicial, informe o **título do normativo** (ex: *Portaria IFSP nº 001, de 01 de janeiro de 2025*)
2. Selecione ou arraste o arquivo **PDF**
3. Clique em **Enviar para processamento**
4. Acompanhe o progresso pelas etapas exibidas na tela:
   - **Extração** — o texto do PDF é lido automaticamente
   - **FAQ / IA** — uma inteligência artificial gera perguntas frequentes sobre o documento
   - **Bookstack** — o rascunho é criado no portal de normas
   - **Concluído** — link para visualizar o rascunho no Bookstack
5. Ao final, o sistema exibe quantas páginas e caracteres foram extraídos. Um aviso em amarelo indica se o PDF pode estar no formato imagem (sem texto), o que pode comprometer a qualidade

> **Atenção:** o PDF enviado fica armazenado permanentemente, mesmo após revogação do normativo.

---

### Papel: Revisor — Publicar um rascunho

1. Acesse o menu **Revisão**
2. Na seção **Rascunhos aguardando revisão**, clique em **Revisar** para abrir o documento no Bookstack e verificar o conteúdo
3. Se o conteúdo estiver correto, clique em **Publicar**, selecione a **prateleira de destino** e confirme
4. Se houver problema no rascunho, clique em **Remover** (apenas administradores)

---

### Papel: Revisor — Revogar um normativo publicado

1. Acesse o menu **Revisão**
2. Na seção **Normativos publicados**, localize o normativo
3. Clique em **Revogar** e confirme
4. O sistema irá:
   - Gerar automaticamente um resumo estruturado (tipo, número, data e objetivo)
   - Criar uma entrada permanente na prateleira **Revogadas** do Bookstack
   - Remover o normativo original do Bookstack
   - Manter o PDF original armazenado com link permanente de download
5. Acompanhe o progresso na barra exibida na tela

---

### Papel: Administrador — Gerenciar usuários

1. Acesse o menu **Usuários**
2. Todos os usuários que já fizeram login aparecem listados com seu papel atual
3. Para alterar o papel, selecione o novo papel no menu ao lado do usuário e clique em **Salvar**
4. A mudança entra em vigor imediatamente, sem necessidade de o usuário fazer logout

> **Papéis disponíveis:** Servidor · Revisor · Administrador

---

### Papel: Administrador — Consultar o log de auditoria

1. Acesse o menu **Log**
2. O log exibe todas as ações realizadas no sistema em ordem cronológica reversa: uploads, publicações, revogações, exclusões e alterações de papel
3. Cada registro mostra data/hora, usuário responsável, tipo de ação e normativo envolvido

---

### Papel: Administrador — Excluir um registro de revogado

1. Acesse o menu **Revisão**, seção **Normativos revogados**
2. Clique em **Remover**, digite `REMOVER` no campo de confirmação e confirme
3. O registro é removido do portal e o PDF original é excluído do armazenamento

> Esta ação é **irreversível**.

---

## Informações técnicas (para técnicos)

### Stack
- **Backend:** Python + FastAPI + Uvicorn
- **Frontend:** Jinja2 + HTMX + Design System GOV.BR
- **Autenticação:** Google OAuth 2.0 — restrito ao Workspace `@ifsp.edu.br`
- **Armazenamento:** AWS S3
- **IA Generativa:** Amazon Bedrock (Claude Haiku)
- **Wiki:** Bookstack (normas.ifsp.edu.br)
- **Infraestrutura:** AWS EC2 + nginx + Let's Encrypt

### Atualizar o código em produção

```bash
ssh -i ~/.ssh/ifsp-normas-key.pem ec2-user@auto.normas.ifsp.edu.br
cd /home/ec2-user/auto-ifsp-normas
git pull
sudo systemctl restart ifsp-normas
```

Para atualizar também o nginx (após mudanças em `nginx/ifsp-normas.conf`):

```bash
sudo cp nginx/ifsp-normas.conf /etc/nginx/conf.d/ifsp-normas.conf
sudo nginx -t && sudo systemctl reload nginx
```

### Monitorar o serviço

```bash
sudo systemctl status ifsp-normas
sudo journalctl -u ifsp-normas -n 100 --no-pager
```

### Revogar todas as sessões em caso de comprometimento

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
# Substitua SESSION_SECRET_KEY no .env com o valor gerado
sudo systemctl restart ifsp-normas
```

### Ambiente de desenvolvimento local

Consulte o arquivo `.env.example` para as variáveis necessárias. Em desenvolvimento, use as flags `MOCK_AUTH`, `MOCK_BOOKSTACK` e `MOCK_S3` para simular os serviços externos sem custo.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# Configure o .env com MOCK_AUTH=true, MOCK_BOOKSTACK=true, MOCK_S3=true
uvicorn app.main:app --reload
```

---

## Diagrama de arquitetura

O arquivo `docs/arquitetura.puml` contém o diagrama de arquitetura do sistema em formato [PlantUML](https://plantuml.com), com ícones oficiais da AWS.

**Para visualizar:**

1. Instale a extensão [PlantUML](https://marketplace.visualstudio.com/items?itemName=jebbs.plantuml) no VS Code
2. Instale as bibliotecas de ícones AWS localmente em `docs/plantuml-libs/`:
   ```bash
   mkdir -p docs/plantuml-libs
   cd docs/plantuml-libs
   git clone https://github.com/awslabs/aws-icons-for-plantuml.git
   ```
3. Abra `docs/arquitetura.puml` e pressione `Alt+D`

> As bibliotecas estão no `.gitignore` e precisam ser instaladas localmente por cada colaborador.

---

## Desenvolvido com

Este sistema foi desenvolvido com auxílio do **[Claude Code](https://claude.ai/code)** (Anthropic). O assistente de IA participou de todo o ciclo: arquitetura, implementação, integrações, hardening de segurança e deploy em produção.

---

*Instituto Federal de Educação, Ciência e Tecnologia de São Paulo — [www.ifsp.edu.br](https://www.ifsp.edu.br)*
