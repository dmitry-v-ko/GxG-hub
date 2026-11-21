# Opportunity Engine — Deep Research: open-source доноры

> **Архивное исследование доноров, не утверждённый стек.** Архитектурные рекомендации ниже относятся к моменту первоначального обзора. После пересмотра Grist и Activepieces выведены из обязательного стека; актуальные решения см. в [текущей минимальной архитектуре](../CURRENT_ARCHITECTURE.md).


**Дата анализа:** 21 ноября 2026 г.  
**Цель:** найти готовые исходные компоненты для системы B2B-роста (контекст → сигналы → гипотезы → исполнение → метрики → выводы), не создавая собственные agent framework, MCP gateway, маркетинговые коннекторы или платформу экспериментов.  
**Результат:** карта 39 релевантных проектов, shortlist из 10 кандидатов, рекомендуемая архитектура и план технических испытаний.

> **Граница исследования.** Это широкий, но не исчерпывающий обзор публично обнаруживаемых решений. На 21.11.2026 нельзя доказать, что найдены буквально все репозитории. Описания функций и лицензий проверялись по официальным README, документации, лицензиям и GitHub-метаданным. Локального развёртывания и тестов end-to-end не проводилось. Возможности, заявленные авторами в README, не являются независимо подтверждённым качеством или безопасностью. Особенно осторожно следует относиться к очень молодым AI-generated репозиториям.

## 1. Решение в одну минуту

**Не искать одну платформу, которая умеет абсолютно всё.** Для Opportunity Engine предпочтителен **тонкий экспериментальный контур** над существующими системами. Главная механика — записать гипотезу и план *до* действия, получить измеряемое наблюдение, привязать его к данным и не превратить слабое свидетельство в «доказанный» маркетинговый закон. При этом инструменты исполнения и аналитики должны быть уже готовыми.

**Первый кандидат на быстрый пилот:** [Grist Core](https://github.com/gristlabs/grist-core) как структурированный журнал + [GTM OS](https://github.com/mgasser/gtm-os) как версия GTM-контекста + отобранные skills из [AI Marketing Skills](https://github.com/ericosiu/ai-marketing-skills) + уже существующие агенты и GxG Hub. Добавлять [Activepieces](https://github.com/activepieces/activepieces) только для детерминированных интеграций, а [Cognee](https://github.com/topoteretes/cognee) — только если обычного извлечения контекста станет недостаточно.

**Самый интересный самостоятельный open-source кандидат на полный growth loop:** [Growth Lab](https://github.com/tsingyuai/growth-lab). Код и методология поддерживают «наблюдение → действие → рефлексия → следующая итерация», но фактические готовые каналы на момент проверки — SEO и преимущественно китайские контент-платформы, поэтому он скорее донор методологии, чем немедленный B2B-фундамент.

**Осторожность с рекламируемым монолитом:** [Outflare](https://github.com/outflareai/outflare) декларирует большой GTM-контур, но README на текущем коммите прямо называет первым работающим модулем self-hosted cold-email sequencer, а дальнейших агентов и missions — следующим этапом. Новому проекту с датой создания 27.10.2026 и ~25 звёздами рано доверять весь Opportunity Engine.

## 2. Что ищем — и чего не ищем

### Обязательные функции
1. **Контекст бизнеса:** ICP, сегменты, офферы, ограничения, решения, источники и история.
2. **Гипотезы и эксперименты:** постановка, критерий успеха до запуска, исполнение, фактические измерения, статус, исход.
3. **Маркетинговые инструменты:** SEO/GSC/GA4, рекламу, CMS, social, CRM, signal intelligence, email и др. — желательно через существующие MCP, API или connectors.
4. **Многоагентная совместимость:** независимые агенты получают минимальный необходимый контекст и возвращают стандартизированные результаты.
5. **Кросс-экспериментальное обучение:** разделение evidence, observation, inference; применимость результата к новому ICP/офферу; обнаружение противоречий.
6. **Self-hosting/исходники/лицензии:** предпочтительно MIT/Apache-2.0. AGPL/GPL возможны как отдельные сервисы при выполнении условий, но не копируются внутрь закрытого продукта без юридической проверки.
7. **Соло-функциональность:** минимум постоянного сопровождения и инфраструктуры.

### Не нужно на первой итерации
- Свой MCP Gateway: GxG Hub уже использует IBM ContextForge как внешнюю зависимость.
- Собственный универсальный orchestrator: использовать существующие агенты и опционально готовый workflow engine.
- Собственный graph DB/RAG stack, fine-tuning, статистическую исследовательскую платформу, CDP и CRM — без доказанного пробела.
- Автоматические действия с рекламными бюджетами и внешними коммуникациями без Action Gate и согласования.

## 3. Кандидаты-лидеры (shortlist)

| Роль | Кандидат | Фактически полезная часть | Ограничение | Решение |
|---|---|---|---|---|
| Журнал экспериментов | **[Grist Core](https://github.com/gristlabs/grist-core)** (Apache-2.0) | реляционные таблицы, связи, UI, формулы, API, self-host; есть чистый OSS-образ `gristlabs/grist-oss` | не имеет встроенной маркетинговой методологии | **Пилотировать первым** |
| Цикл growth-learning | **[Growth Lab](https://github.com/tsingyuai/growth-lab)** (Apache-2.0) | Model/Collector/Executor Skills, раздельная Memory каждого цикла, observe–act–review | покрывает конкретные SEO и китайские контентные каналы; не полноценный CRM/outbound | **Изучить как главный архитектурный донор** |
| Эксперименты в коде | **[AI Marketing Skills / Growth Engine](https://github.com/ericosiu/ai-marketing-skills/blob/main/growth-engine/SKILL.md)** (MIT) | create/log/score/suggest, playbook и weekly scorecard; исходные скрипты | рассчитан прежде всего на вариантные тесты; автоматические «победители» не следует переносить на малые B2B-выборки | **Взять модели данных/CLI-логику, проверить статистику** |
| Company Context | **[GTM OS](https://github.com/mgasser/gtm-os)** (MIT) | единый master knowledgebase в Git, ICP/persona/positioning, контекстные экстракты для агентов | шаблон, 1 commit/минимальная история, не готовый backend | **Использовать шаблоны, не развёртывать как сервис** |
| Skills/методология | **[Marketing Agent OS](https://github.com/iamwaqargulzar/Marketing-Agent-OS)** (Apache-2.0 wrapper + лицензии upstream) | 238 нормализованных skills, selective install, provenance, правила evidence & approvals | инструкция ≠ рабочая интеграция; upstream-лицензии индивидуальны | **Кураторски заимствовать 5–15 skills** |
| Интеграции/автоматизация | **[Activepieces Community](https://github.com/activepieces/activepieces)** (MIT core, EE отдельно) | визуальные flows, большой каталог pieces, MCP | не заменяет growth experiment ledger; проверять лицензии функций | **Добавить при реальной необходимости** |
| Signals | **[changedetection.io](https://github.com/dgtlmoon/changedetection.io)** (Apache-2.0) | мониторинг сайтов, webhooks, REST/MCP, diff истории | само изменение не равно сигналу спроса | **Сильный донор Market Signal Radar** |
| Web research | **[Crawl4AI](https://github.com/unclecode/crawl4ai)** (Apache-2.0 с дополнительной attribution requirement) | web-to-Markdown/JSON, self-hosted crawler, API/агентские инструменты | сайты/robots/условия доступа и операционное сопровождение | **Для ограниченного сбора открытых данных** |
| Рекламные коннекторы | **[MCP Ads](https://github.com/RoyAzran/mcp-ads)** (MIT) | рекламные + GA4/GSC инструменты из MCP, self-host | новый небольшой проект; доступ к API самих платформ требует отдельного одобрения | **Пилот только в read-only scope** |
| Долговременная память | **[Cognee](https://github.com/topoteretes/cognee)** (Apache-2.0 core) | документы/разговоры → memory graph; MCP/SDK/API | сложнее простой базы; часть production-ready graph-on-Postgres заявлена как лицензируемая | **Добавлять только после benchmark** |

### Важные кандидаты второго эшелона
- [Outflare](https://github.com/outflareai/outflare) (AGPL-3.0): модуль outbound-кампаний, коннекторы и API; ранняя стадия, не полноценный universal GTM loop.
- [OpenGrow](https://github.com/opengrowhq/opengrow) (AGPL-3.0): действительно описывает цепочку *контекст → контент → публикация → атрибуция → рекомендации → следующий запуск*, но сам репозиторий отмечает **pre-alpha**, новые источники дистрибуции ещё требуют валидации.
- [Duct](https://github.com/5hirish/duct) (MIT): хороший образец growth-аналитика с несколькими аккаунтами и human approval; молодой проект, необходимо проверять каждый адаптер.
- [GrowthBook](https://github.com/growthbook/growthbook) (MIT core + proprietary enterprise): зрелый специализированный A/B engine с REST API/MCP/SDK, не общий B2B-журнал гипотез.
- [CreativeLift AI](https://github.com/Hiberius/creativelift-ai) (Apache-2.0): рекламные creative treatments, event ingestion, статистика, approval, атрибуция; очень ранний проект, заявленные автором тесты не запускались в этом исследовании.

## 4. Longlist — 39 обнаруженных проектов по категориям

**Условные обозначения:** «готовый механизм» = исходники содержат заявленную функцию по документации автора, **не** означает production-аудит. «Донор» — применять ограниченный компонент, не всю систему.

### A. Комплексные Growth/Marketing AI-системы (11)

| # | Проект | Что даёт | Лицензия/статус | Вердикт |
|---|---|---|---|---|
| 1 | [Growth Lab](https://github.com/tsingyuai/growth-lab) | наблюдение/действие/рефлексия; persistent Model Memory | Apache-2.0; растущий проект | донор learning loop |
| 2 | [AI Marketing Skills](https://github.com/ericosiu/ai-marketing-skills) | Growth Engine, ICP learner, pipeline/outbound/content tools | MIT | донор экспериментального кода и skills |
| 3 | [Marketing Agent OS](https://github.com/iamwaqargulzar/Marketing-Agent-OS) | 238 portable skills, evidence policy, provenance | Apache-2.0 wrapper; upstream отдельно | skill-слой |
| 4 | [Marketing Skills OS](https://github.com/scayver/marketing-skills) | 76 skills, 347 integration guides, 62 CLI tools (по README) | MIT | skills и рецепты, не backend |
| 5 | [GrowthOS](https://github.com/scottjs12/GrowthOS) | 13 агентов, experiment cycle, клиентские папки, pattern library | репозиторий с открытым кодом; лицензию сверять в LICENSE | процессы и handoff, не orchestrator для production |
| 6 | [GTM OS](https://github.com/mgasser/gtm-os) | Git-first B2B контекст и scoped files | MIT; крошечный репозиторий | Company Context-шаблоны |
| 7 | [NoCodeWork Growth OS](https://github.com/nocodework/growth-os) | onboarding, GA4/GSC/audits, Hermes+MCP | MIT; маленький репозиторий | адаптерный паттерн |
| 8 | [Outflare](https://github.com/outflareai/outflare) | outbound, mailbox pools, suppression, API/MCP, brand context | AGPL-3.0; early development | точечный outbound-донор |
| 9 | [OpenGrow](https://github.com/opengrowhq/opengrow) | контент, публикация, attribution, suggestions → new run | AGPL-3.0; pre-alpha | донор content loop |
| 10 | [Duct](https://github.com/5hirish/duct) | маркетинговый аналитик для нескольких API | MIT; ранний проект | интерфейс analytics agent |
| 11 | [CreativeLift AI](https://github.com/Hiberius/creativelift-ai) | creative experiment trace, lift, approvals | Apache-2.0; очень ранний | донор идеи evidence/measurement |

### B. Эксперименты, измерение, знания (7)

| # | Проект | Что даёт | Лицензия/статус | Вердикт |
|---|---|---|---|---|
| 12 | [GrowthBook](https://github.com/growthbook/growthbook) | A/B/statistics/flags/metrics/API/MCP | MIT core + Enterprise License | только когда нужен контролируемый A/B |
| 13 | [PostHog FOSS](https://github.com/PostHog/posthog-foss) / [PostHog](https://github.com/PostHog/posthog) | продуктовая/веб-аналитика и эксперименты | MIT OSS части + отдельные ee; self-host limitations | данные и UI; проверить feature parity OSS |
| 14 | [Grist Core](https://github.com/gristlabs/grist-core) | реляционный экспериментальный журнал, связанные таблицы, API | Apache-2.0 | стартовый system of record |
| 15 | [Twenty](https://github.com/twentyhq/twenty) | open-source CRM, custom objects, REST/GraphQL | AGPL-3.0 core + EE + application exception | если CRM нужна как основной контекст |
| 16 | [Langfuse](https://github.com/langfuse/langfuse) | traces/evals/agent calls/prompt versions | MIT core + EE | качество работы агентов, не коммерческая эффективность |
| 17 | [Umami](https://github.com/umami-software/umami) | простая web/product analytics, conversions, campaign data | MIT | лёгкий измерительный слой |
| 18 | [Matomo](https://github.com/matomo-org/matomo) | аналитика, attribution/кампании и API | GPLv3+ core, платные плагины | опционально, если есть строгая privacy-потребность |

### C. Интеграции, каналы и исполнение (10)

| # | Проект | Что даёт | Лицензия/статус | Вердикт |
|---|---|---|---|---|
| 19 | [Activepieces](https://github.com/activepieces/activepieces) | connectors, workflow automation, MCP | MIT community + EE | выполнять детерминированные операции |
| 20 | [MCP Ads](https://github.com/RoyAzran/mcp-ads) | Google/Meta/GA4/GSC и др. через MCP | MIT; малый проект | pilot read-only, затем approval-gated writes |
| 21 | [Google Ads MCP](https://github.com/googleads/google-ads-mcp) | официальный агентский доступ к Google Ads | проверить текущую лицензию в repo | если нужен один рекламный провайдер |
| 22 | [Meta Ads MCP](https://github.com/pipeboard-co/meta-ads-mcp) | Meta Ads tools | проверить текущую лицензию в repo | провайдер-специфичная альтернатива |
| 23 | [Postiz](https://github.com/gitroomhq/postiz-app) | social publishing, scheduling, analytics, API | AGPL-3.0 | отдельный канал, не ядро |
| 24 | [Mautic](https://github.com/mautic/mautic) | multi-channel marketing automation, campaigns, API | GPLv3 | отдельный lifecycle-механизм |
| 25 | [Dittofeed](https://github.com/dittofeed/dittofeed) | event-driven email/SMS/push journeys | MIT core; часть multi-tenant/embedding закрыта | альтернатива Mautic для событийных journey |
| 26 | [listmonk](https://github.com/knadh/listmonk) | newsletter/campaign/subscriber API | AGPL-3.0 | лёгкий email-канал |
| 27 | [SerpBear](https://github.com/towfiqi/serpbear) | keyword rank tracking + GSC данные | MIT | SEO измерение, не GTM core |
| 28 | [RSSHub](https://github.com/DIYgod/RSSHub) | унификация лент из многих публичных источников | AGPL-3.0 | входящие market signals |

### D. Сбор сигналов, research, память (11)

| # | Проект | Что даёт | Лицензия/статус | Вердикт |
|---|---|---|---|---|
| 29 | [changedetection.io](https://github.com/dgtlmoon/changedetection.io) | watch + diff + webhook + REST/MCP | Apache-2.0 | мониторинг конкурентов и сайтов |
| 30 | [Crawl4AI](https://github.com/unclecode/crawl4ai) | crawling/Markdown/JSON | Apache-2.0 с дополнительной attribution clause | извлечение публичного контекста |
| 31 | [Firecrawl](https://github.com/firecrawl/firecrawl) | crawl/extract/search/MCP | AGPL-3.0 core + облачные отличия | альтернатива Crawl4AI |
| 32 | [SearXNG](https://github.com/searxng/searxng) | self-hosted meta-search | AGPL-3.0 | независимый search-источник |
| 33 | [Folo](https://github.com/RSSNext/Folo) | RSS + AI summaries + newsletters | AGPL-3.0, исключение для иконок | UI-monitoring, не API ядро |
| 34 | [Cognee](https://github.com/topoteretes/cognee) | graph memory, MCP/REST/SDK, Company Brain | Apache-2.0 core + лицензируемые production возможности | позже, если нужен RAG/graph |
| 35 | [Graphiti](https://github.com/getzep/graphiti) | temporal graph, episodes, provenance, retrieval | проверить лицензию конкретных компонентов | только через безопасный backend |
| 36 | [EverRoom](https://github.com/NxcoreAI/EverRoom) | local-first context rooms, evidence ledger, agent memory | Apache-2.0 основного проекта; зависимости проверить | личный workspace, не multi-tenant core |
| 37 | [LangExtract](https://github.com/google/langextract) | извлечение структурированных утверждений с источниками | проверить текущую лицензию в repo | evidence-ingestion компонент |
| 38 | [Activepieces Pieces](https://github.com/activepieces/activepieces/tree/main/packages/pieces) | открытые исходники многочисленных интеграций | MIT, кроме отдельно помеченных EE | повторно использовать коннекторы, не переписывать |
| 39 | [GTM Superintelligence](https://github.com/attentiontech/gtm-superintelligence) | анализ sales conversations, CRM updates, evidence snippets | Apache-2.0 по публичной странице; проверить ограничение deploy | возможный донор win/loss intelligence |

## 5. Главные выводы из проверки исходных проектов

### 5.1 Готового «универсального маркетингового Growth Engine» из зрелого FOSS не подтверждено
Сильнейшие специализированные компоненты **не содержат всей** последовательности «рынок → гипотеза → исполнение → CRM-пайплайн → доказательный вывод → выбор следующей гипотезы» в единой зрелой архитектуре. Growth Lab и OpenGrow наиболее явно декларируют сквозной цикл, но покрывают конкретные каналы и имеют ограничения зрелости.

### 5.2 Для B2B нужен экспериментальный журнал, а не только A/B-движок
При низких объёмах сделок нельзя автоматически переносить критерии `p<0.05` и `+15% lift` из generic Growth Engine на десятки outbound-писем. Сохраняйте результат как наблюдение, поддерживайте сравнимые контрольные группы при возможности, фиксируйте ограничения и время до сделки. GrowthBook нужен **только при достаточном трафике и контролируемом assignment**.

### 5.3 У «skills OS» и «connectors OS» разный тип ценности
Skills содержат метод, шаблоны, последовательность работы и критерии качества. Коннекторы реально получают и изменяют данные. 238 skills не означают 238 работающих API-интеграций; 347 guides — это не 347 протестированных коннекторов. Парадигма Opportunity Engine требует оба уровня, но их независимости.

### 5.4 Редкость — не синоним преимущества
GTM OS (около одного commit и одной звезды на момент проверки) может иметь очень хорошие шаблоны, но это не production-платформа. Более молодые репозитории могут быть полезными донорами фрагментов, но нуждаются в тестах, безопасности и плане замены.

### 5.5 Лицензии — часть выбора архитектуры
Нельзя автоматически смешивать GPL/AGPL код с произвольным закрытым продуктом. Самостоятельный сетевой сервис через стабильный API и копирование кода внутрь единого продукта имеют разные последствия. NocoDB (Sustainable Use License с ограничением на коммерческое использование по версии 2026) и n8n (fair-code) исключены из строгого open-source core. Для Crawl4AI нужно учитывать дополнительное условие атрибуции, указанное в LICENSE.

## 6. Рекомендуемая архитектура пилота

```text
[Company GTM Context — Git/Markdown (GTM OS шаблоны)]
                   |
[Growth Knowledge — selected Agent Skills + playbooks]
                   |
[Experiment Registry — Grist Core]
  Hypothesis / Test Plan / Assignment / Evidence /
  Metrics / Outcome / Decision / Learning
                   |
        [Existing agent(s): Hermes, Claude, etc.]
                   |
 [GxG Hub (IBM ContextForge, policy, Action Gate)]
                   |
     [Разрешённые MCP/API-коннекторы]
      /              |                \
 Signals         Marketing          Analytics
 changedetection  Activepieces*      GSC / GA4 / CRM
 Crawl4AI         Postiz*            Umami*
 MCP Ads*         Mautic*            GrowthBook*
                   |
          [Structured observation]
                   |
          [Experiment Registry]
                   |
        [Evidence-based review]
                   |
        [Next hypothesis proposal]
```
`*` — подключается **по мере потребности**, не входит в обязательный MVP.

**Данные экспериментов должны принадлежать registry, а не агенту.** GxG Hub — доступ и политика вызовов; данные живут в своих специализированных системах. Рекомендации и выводы должны храниться вместе с идентификаторами источников, методикой измерения и статусом доказательности.

### Минимальный контракт эксперимента

```yaml
id: exp-001
tenant_id: own-business
hypothesis: "Компании с событием X чаще соглашаются на встречу, чем сопоставимые компании без события X"
context_refs: [company-context-v1, icp-b2b-consulting]
audience: "B2B consulting companies; decision-maker: revenue lead"
trigger: "observable public event X"
offer: "specific value proposition"
method: "outreach or landing-page test"
pre_registered_success_metric: "qualified conversations per eligible company"
measurement_window_days: 30
assets: []
executor: hermes-agent
approval_required: true
observations: []
evidence_refs: []
analysis_status: not_started
inference_status: unverified
decision: pending
next_experiment: null
```

Это **минимальный собственный доменный формат** поверх готовой базы, не собственный движок экспериментов. Статистические расширения подключаются только к дизайнам, которым они подходят.

## 7. Три архитектурные альтернативы

| Сценарий | Сборка | Плюсы | Минусы | Приоритет |
|---|---|---|---|---|
| **A. Lean / Solo** | Grist + GTM OS templates + 5–15 skills + GxG Hub + Hermes | минимум кода и серверов; многоплатформенность | больше ручной проверки результата | **№1** |
| **B. Channel-first** | A + Activepieces + MCP Ads + changedetection.io + Postiz/Mautic | готовые интеграции и execution | сервисов больше; approval/secrets/согласия | №2, по потребности |
| **C. Memory/Intelligence-first** | A + Cognee или Graphiti + Langfuse + GrowthBook в A/B кейсах | расширенный retrieval и оценка AI | дорогая эксплуатация и сложность, до доказанной пользы | позже |

**Не комбинировать два оркестратора и два слоя памяти без подтверждённой нужды.** Если Hermes нормально планирует и исполняет задачи, Activepieces — только для повторяемых интеграционных workflows. Если Git/Grist уже дают нужную историю, Cognee/Graphiti пока не нужен.

## 8. План валидации доноров — конкретные испытания

### Тест 1. Single source of truth
Один ICP и один оффер записываются в GTM-context, используются Hermes и другим агентом, затем меняются. Критерий: оба агента получают актуальное scoped-представление, а старые версии доступны и не перезаписываются бесследно.

### Тест 2. Experiment registry
Создать 3 гипотезы, один тест остановить, один признать неопределённым, один продолжить. Критерий: API/GUI извлекает статус, план, результат, метрику, материал, доказательство, решение — без поиска по свободным текстам.

### Тест 3. Marketing connectors
Через GxG Hub выполнить **только чтение** GSC/GA4/CRM и мониторинг одного сайта. Критерий: корректный ответ, источники, дата получения, audit, отсутствие прав записи.

### Тест 4. Agent handoff
Hermes создаёт brief, другой агент производит материал, результат связывается с исходным experiment_id, не теряется при смене агента. Критерий: человек может восстановить цепочку действий.

### Тест 5. Evidence-based learning
Система получает три противоречащих исхода, должна сохранить несовпадение и предложить ограниченную проверку, **не** объявлять «доказано». Критерий: выводы с ссылками на data и уровнем уверенности.

### Тест 6. Separation
Два клиента с близкими гипотезами: доступ к записи другого клиента и необоснованный перенос конфиденциальных данных должны быть запрещены. Сначала отдельные инстансы/базы, если isolation не доказана.

### Тест 7. No vendor lock-in
Замена агентского runtime и export experiment ledger в стандартный формат. Критерий: потеря полезной истории = 0.

## 9. Решение по разработке (build / adapt / integrate)

**Интегрировать, не писать:** собственный MCP gateway, universal orchestrator, web crawler, website watch, keyword rank tracker, social scheduler, CRM, email platform, feature flag/stats engine.

**Адаптировать:** GTM OS master-context и scoped-context, selected marketing skills, Growth Lab observe-act-review pattern, CLI экспериментального Growth Engine, стандартные CRM/ads metrics.

**Собственное — максимально узко:** единый контракт эксперимента; links между hypothesis/asset/source/result; правила статусов вывода (unverified → tentative → supported/contradicted); бизнес-логика выбора следующей проверки; безопасная доставка минимального контекста агентам. Даже эти функции сначала реализуются **конфигурацией таблиц и skills**, а не новой платформой.

## 10. Итог и порядок следующих решений

1. **Начать с Grist Core** как экспериментального журнала, не с memory graph и не с собственного Growth OS.
2. **Взять структуру GTM OS** как Git-версионируемый контекст, адаптировать к B2B-услугам.
3. **Исследовать в коде Growth Lab и `ericosiu/growth-engine`**, сравнить способ сохранения исходов, воспроизводимость, real integrations и ограничения статистики.
4. **Выбрать до 5 базовых skills**, исключить пересечения. Не устанавливать сотни skills всем агентам сразу.
5. **Провести одно реальное B2B-испытание** через GxG Hub и существующих агентов. Метрика — квалифицированные диалоги/коммерческие возможности, а не число отправок.
6. **Только после первого цикла** решить, нужна ли отдельная система долгосрочной памяти, массовые connectors или A/B engine.

> **Стратегический тезис:** сам growth-процесс не настолько уникален, чтобы оправдать строительство собственной инфраструктуры. Потенциально незаменимое преимущество Opportunity Engine — дисциплина выбора экспериментов, коммерческий контекст и накопленные проверяемые результаты, которые переживают замену моделей, агентов и каналов.

## 11. Прямые первоисточники, использованные для проверки

- [Growth Lab README (включая разделы Current Status и License)](https://github.com/tsingyuai/growth-lab); [English README](https://github.com/tsingyuai/growth-lab/blob/main/README.en.md).
- [AI Marketing Skills Growth Engine SKILL.md](https://github.com/ericosiu/ai-marketing-skills/blob/main/growth-engine/SKILL.md); [README и лицензия](https://github.com/ericosiu/ai-marketing-skills).
- [Marketing Agent OS README/THIRD_PARTY_LICENSES](https://github.com/iamwaqargulzar/Marketing-Agent-OS).
- [GTM OS README](https://github.com/mgasser/gtm-os).
- [Grist Core README](https://github.com/gristlabs/grist-core); [`gristlabs/grist-oss` image explanation](https://github.com/gristlabs/grist-core/blob/main/README.md).
- [Activepieces licensing](https://github.com/activepieces/activepieces/blob/main/LICENSE); [MCP Overview](https://github.com/activepieces/activepieces/blob/main/docs/mcp/overview.mdx).
- [Outflare README](https://github.com/outflareai/outflare); [Outflare licensing](https://outflare.ai/developers/open-source/).
- [OpenGrow README](https://github.com/opengrowhq/opengrow).
- [GrowthBook README/license](https://github.com/growthbook/growthbook).
- [PostHog self-hosting disclaimers](https://github.com/PostHog/posthog.com/blob/master/contents/docs/self-host/open-source/disclaimer.mdx).
- [MCP Ads README](https://github.com/RoyAzran/mcp-ads).
- [Cognee README](https://github.com/topoteretes/cognee); [Graphiti MCP security warning](https://github.com/getzep/graphiti/blob/main/mcp_server/README.md).
- [changedetection.io README](https://github.com/dgtlmoon/changedetection.io); [API spec](https://github.com/dgtlmoon/changedetection.io/blob/master/docs/api-spec.yaml).
- [Crawl4AI License](https://github.com/unclecode/crawl4ai/blob/main/LICENSE); [Firecrawl License](https://github.com/firecrawl/firecrawl).
- [Postiz README](https://github.com/gitroomhq/postiz-app); [Mautic License](https://github.com/mautic/mautic/blob/7.x/LICENSE.txt); [listmonk README](https://github.com/knadh/listmonk/blob/master/README.md); [Dittofeed README](https://github.com/dittofeed/dittofeed).
- [Twenty License](https://github.com/twentyhq/twenty/blob/main/LICENSE); [NocoDB Sustainable Use License](https://github.com/nocodb/nocodb/blob/develop/LICENSE.md).
- [SerpBear README](https://github.com/towfiqi/serpbear); [Umami README](https://github.com/umami-software/umami); [Langfuse open-source overview](https://langfuse.com/resources/engineering/open-source-llm-observability).

**Не выполнено:** установка, smoke/E2E тесты, воспроизведение claimed benchmarks, юридическая экспертиза всех транзитивных лицензий и проверка compliance внешних рекламных/социальных API. Это обязательные следующие этапы перед эксплуатацией у клиентов.