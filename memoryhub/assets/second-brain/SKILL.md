---
name: second-brain
description: Consulta la conoscenza accumulata nei grafi LLM Wiki locali quando un task richiede architettura, fonti, relazioni o contesto storico di un progetto. Usala in Claude Code e Codex senza ricostruire o modificare i grafi.
---

# Second Brain

Usa LLM Wiki come livello di conoscenza consultabile e Memory Hub come memoria
operativa dei task. Non confondere i due ruoli.

## Scegliere il progetto

1. Preferisci il progetto specifico `prj-<slug>` che corrisponde al repository o
   al cliente corrente.
2. Se il task è trasversale, usa l'area pertinente, per esempio `product-dev`,
   `marketing`, `operations`, `sales`, `finance`, `rnd` o `machine-map`.
3. Se l'associazione non è evidente, chiama `llm_wiki_projects` prima della
   ricerca. Non unire implicitamente conoscenza di clienti o progetti diversi.

## Consultare la conoscenza

- Usa `llm_wiki_search` per fatti, documenti e pagine pertinenti.
- Usa `llm_wiki_graph` quando servono entità e relazioni.
- Usa `llm_wiki_read_file` soltanto per approfondire una pagina già individuata.
- Indica il progetto e, quando disponibile, il percorso della pagina usata.
- Verifica sempre le informazioni rilevanti contro istruzioni correnti, file,
  Git e test: il grafo è contesto accumulato, non verità assoluta.

## Branch e freschezza

- Considera LLM Wiki la rappresentazione del branch canonico configurato,
  normalmente `main` o `master`.
- Se stai lavorando su un altro branch, usa file e diff Git come verità del
  branch corrente e il grafo come baseline canonica. Non richiedere una
  reindicizzazione automatica del branch di lavoro.
- Prima di basare una decisione importante sul grafo, controlla la pagina
  `wiki/memoryhub-freshness.md` o esegui `memoryhub brain-doctor` quando
  disponibile.
- Un aggiornamento del grafo deve riportare commit canonico, modalità
  incrementale/riconciliazione e canary verificato in file, ricerca e grafo.

## Limiti operativi

- La consultazione è read-only: non richiedere rescan, ingest o ricostruzioni a
  meno che l'utente lo chieda esplicitamente.
- Non salvare credenziali, token, cookie, chiavi o valori grezzi di `.env`.
- Se LLM Wiki locale non risponde, dichiaralo brevemente e continua usando
  Memory Hub e le evidenze del workspace. Il guasto del second brain non deve
  bloccare la continuità operativa.
