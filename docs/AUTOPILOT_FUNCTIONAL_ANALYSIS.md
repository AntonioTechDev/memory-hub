# Memory Hub Autopilot — analisi funzionale

## Scopo

Autopilot trasforma un obiettivo espresso una sola volta in un lavoro locale,
recuperabile e verificato eseguito da sessioni Codex e Claude bounded. Memory Hub
rimane la fonte dello stato; il runner mantiene vivi i job; un orchestratore
stateless decide il prossimo passo; i worker modificano codice in worktree
isolati. Nessun componente apre listener o usa memoria remota.

## Esperienza utente

L'ingresso primario e' la skill `autopilot`:

```text
$autopilot Completa il refactor e considera finito il lavoro quando tutti i test passano.
```

La skill crea il job attraverso Memory Hub e restituisce subito ID, piano
iniziale e stato. L'equivalente CLI, utile per test e client privi di skill, e':

```bash
memoryhub autopilot start --objective "..."
```

`memoryhub autopilot status` e la normale memoria operativa permettono a una
nuova chat Codex o Claude di osservare e riprendere il lavoro.

## Requisiti funzionali

### AF1 — Goal contract

Il prompt viene normalizzato in un contratto con obiettivo, definition of done,
vincoli, non-obiettivi, classe di rischio e budget. Un lavoro banale rimane un
solo task; un task esiste soltanto quando produce un risultato validabile
indipendente.

### AF2 — Task contract

Ogni task dichiara dipendenze, scope consentito, vincoli, criteri di
accettazione, validazioni, profilo esecutivo e condizioni di arresto. Il prompt
del worker viene compilato da questo contratto, dalle istruzioni del repository
e dal contesto Memory Hub verificato.

### AF3 — Routing proporzionato

I profili astratti `fast`, `builder`, `senior` e `lead` vengono mappati sulle
opzioni native disponibili. La selezione considera eleggibilita', usage,
rischio, carico e risultati locali precedenti. L'effort minimo adeguato viene
escalato soltanto dopo una validazione fallita o un problema ambiguo.

### AF4 — Usage e circuit breaker

Gli adapter normalizzano l'output dei comandi usage dei provider in
`available`, `constrained`, `rate_limited`, `unavailable` e, quando presente,
registrano il reset. Un provider limitato non viene interrogato ripetutamente.
Non si passa mai automaticamente a consumo API a pagamento.

### AF5 — Runner recuperabile

Il runner usa lease, heartbeat, attempt e transizioni atomiche. Una decisione
dell'orchestratore viene applicata solo dopo output schema-valido. Dopo crash o
timeout, un nuovo processo riconcilia database, PID, Git e worktree e riparte
dall'ultimo stato verificabile, non dal ragionamento volatile del modello.
Il percorso sorgente e il task Memory Hub sono immutabili e distinti per job:
un hook partito da un worktree effimero non puo' sostituire il progetto canonico.

### AF6 — Parallelismo controllato

Il default e' un worker. Il limite assoluto e' due, solo per task indipendenti
con scope non sovrapposti. Ogni worker usa un worktree separato; integrazione e
validation gate restano sequenziali.

### AF7 — Fallback

Un attempt puo' terminare `succeeded`, `blocked`, `failed`, `timed_out`,
`rate_limited` o `interrupted`. Dopo un fallimento si prova una sessione fresca;
dopo due approcci equivalenti si cambia provider/profilo o si blocca il job. Un
worker sostitutivo parte solo dopo il rilascio del lease precedente.
Se un worker ha gia' prodotto modifiche nello scope e il solo blocco e'
l'impossibilita' del sandbox headless di eseguire una validazione allow-listed,
il runner puo' eseguirla direttamente. Il recupero e' marcato nelle prove e non
sostituisce il reviewer finale.

### AF8 — Validation gate

Il self-report del worker non basta. Il runner esegue comandi deterministici e
un reviewer fresco risponde a: goal soddisfatto, criteri coperti, diff minimo,
regressioni, test pertinenti, scope rispettato, rischi residui e sicurezza
dell'integrazione. Solo prove verdi possono chiudere il job.

### AF9 — Protezione dall'over-engineering

La classe `xs` produce un solo task, profilo `fast` e validazione mirata. Ogni
task deve mappare a un criterio del goal; ogni validazione deve proteggere un
fallimento plausibile. Nuove dipendenze, astrazioni speculative, refactor
collaterali e scope oltre il doppio del piano richiedono replanning.

### AF10 — Comunicazione

Gli eventi visibili sono: avvio, piano, milestone, fallback, blocco e risultato
finale. Se la chat e' chiusa, il job continua e il riepilogo viene recuperato da
Memory Hub alla sessione successiva.
Il log detached contiene una riga JSON flushed per ogni milestone, quindi resta
osservabile anche quando il runner viene interrotto prima del report finale.

## Stato e transizioni

```text
draft -> planning -> running -> validating -> completed
                    |    ^          |
                    |    +-- replan-+
                    +-> paused / blocked / failed
```

Le scritture sono idempotenti. Un job non puo' passare a `completed` senza un
validation gate verde; un task non puo' partire senza dipendenze concluse e
lease disponibile.

## Modello dati

- `autopilot_jobs`: goal contract, stato, piano, limiti e heartbeat del runner;
- `autopilot_tasks`: task contract, dipendenze, routing e stato;
- `autopilot_runs`: provider/model/effort, PID, attempt, risultato e prove;
- `provider_usage`: snapshot normalizzato e circuit breaker.

La migrazione e' additiva e forward-only; le tabelle Phase 1 esistenti non
vengono riscritte.

## Vincoli di sicurezza

- locale e stdio, nessun listener;
- report privati, bounded e redatti;
- nessun push, deploy, migrazione remota o acquisto crediti implicito;
- nessuna modifica simultanea nello stesso worktree;
- gli hook Memory Hub dei worker effimeri sono soppressi: persiste solo il
  checkpoint bounded scritto dal runner;
- scope con glob, validazioni allow-listed e limite tentativi sono verificati
  dal runner, non affidati al self-report del modello;
- operazioni esterne non idempotenti richiedono riconciliazione umana;
- la memoria non concede permessi aggiuntivi.

## Fuori scope MVP

Dashboard web, server centrale, piu' di due worker, scheduler distribuito,
multiutente, routing ML, deployment automatico e supporto dichiarato per OS non
validati.

## Criteri di accettazione release

1. Goal banale resta un task senza parallelismo o reviewer superfluo.
2. Goal complesso produce task contract completi e dipendenze acicliche.
3. Routing rispetta usage, profilo, riserva lead e fallback.
4. Due task disgiunti possono avanzare; scope sovrapposti vengono serializzati.
5. Crash di orchestratore, worker e runner non perde stato committed.
6. Timeout non lascia processi, lease o worktree orfani.
7. Test falliti impediscono `completed` e producono un replan concreto.
8. Codex e Claude leggono lo stesso stato e possono sostituirsi.
9. Skill e installazione clean-room sono idempotenti.
10. Suite locale, stress e almeno uno smoke reale per provider disponibile sono
    documentati con evidenza onesta.
