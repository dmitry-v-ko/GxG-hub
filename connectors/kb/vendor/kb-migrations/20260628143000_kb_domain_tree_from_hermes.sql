-- Sync KB Domain taxonomy from Hermes domain_tree.yaml.
-- Source: /Users/dk1/go/Hermes/skills/knowledge-base/config/kb-architecture/domain_tree.yaml

BEGIN;

CREATE TEMP TABLE desired_domain_terms (
  key text PRIMARY KEY,
  label text NOT NULL,
  description text,
  parent_key text,
  weight integer NOT NULL
) ON COMMIT DROP;

INSERT INTO desired_domain_terms (key, label, description, parent_key, weight)
VALUES
  ('demand-gen', 'Demand Generation', 'Все активности по привлечению и конвертации лидов — inbound и outbound.', NULL, 1),
  ('inbound-marketing', 'Inbound Marketing', 'Привлечение через контент, SEO, органику и brand awareness.', 'demand-gen', 101),
  ('seo-organic', 'SEO & Organic', 'Оптимизация под поисковый спрос, органический трафик и ссылочная масса.', 'inbound-marketing', 201),
  ('brand-awareness', 'Brand & Awareness', 'Позиционирование, PR, community building и thought leadership.', 'inbound-marketing', 202),
  ('events-webinars', 'Events & Webinars', 'Онлайн и офлайн мероприятия как канал лидогенерации и прогрева.', 'inbound-marketing', 203),
  ('outbound', 'Outbound Prospecting', 'Исходящий поиск и квалификация потенциальных клиентов.', 'demand-gen', 102),
  ('cold-outreach', 'Cold Outreach', 'Email, LinkedIn и мультиканальные холодные последовательности.', 'outbound', 201),
  ('abm', 'Account-Based Marketing', 'Персонализированные кампании на целевые аккаунты (1:1, 1:few, 1:many).', 'outbound', 202),
  ('signal-prospecting', 'Signal-Based Prospecting', 'Триггерный аутрич на основе intent-сигналов и data-enrichment.', 'outbound', 203),
  ('paid-acquisition', 'Paid Acquisition', 'Платные каналы привлечения — paid social, PPC, influence, affiliate.', 'demand-gen', 103),
  ('paid-social', 'Paid Social', 'Таргетированная реклама в LinkedIn, Meta, YouTube и других соцсетях.', 'paid-acquisition', 201),
  ('ppc-sem', 'PPC & SEM', 'Контекстная и поисковая реклама Google, Bing и аналогов.', 'paid-acquisition', 202),
  ('influencer-affiliate', 'Influencer & Affiliate', 'Партнёрский маркетинг через блогеров, KOL и аффилиатов.', 'paid-acquisition', 203),
  ('content-ops', 'Content Operations', 'Создание, управление и дистрибуция контента как канала роста и доверия.', NULL, 2),
  ('content-strategy', 'Content Strategy', 'ICP-based планирование тем, форматов, editorial calendar и контент-воронки.', 'content-ops', 101),
  ('content-creation', 'Content Creation', 'Производство текста, видео, подкастов и визуального контента.', 'content-ops', 102),
  ('written-content', 'Written Content', 'Статьи, кейсы, white papers, thought leadership и long-form SEO.', 'content-creation', 201),
  ('video-audio', 'Video & Audio', 'Видеоролики, подкасты, вебинары, short-form видео (reels, shorts).', 'content-creation', 202),
  ('social-content', 'Social Media Content', 'Posts, threads, carousels, stories — адаптация под форматы соцсетей.', 'content-creation', 203),
  ('content-distribution', 'Content Distribution', 'Каналы публикации, продвижения и переупаковки готового контента.', 'content-ops', 103),
  ('email-newsletter', 'Email & Newsletter', 'Рассылки, nurture-последовательности, newsletter как owned media.', 'content-distribution', 201),
  ('social-channels', 'Social Channels', 'Публикации и органический рост в LinkedIn, X, Instagram и других.', 'content-distribution', 202),
  ('syndication-repurpose', 'Syndication & Repurpose', 'Переупаковка контента, репосты, guest posting, content atomization.', 'content-distribution', 203),
  ('sales', 'Sales', 'Все процессы превращения лидов в клиентов — от первого касания до закрытия.', NULL, 3),
  ('sales-development', 'Sales Development (SDR)', 'Квалификация лидов и выход на ЛПР — outbound и inbound SDR.', 'sales', 101),
  ('lead-qualification', 'Lead Qualification', 'ICP-скоринг, BANT/MEDDIC-фреймворки, квалификация входящих заявок.', 'sales-development', 201),
  ('outreach-sequences', 'Outreach Sequences', 'Мультиканальные последовательности касаний для назначения встреч.', 'sales-development', 202),
  ('personalization', 'AI Personalization', 'Гиперперсонализация сообщений с помощью AI и data-enrichment.', 'sales-development', 203),
  ('pipeline-management', 'Pipeline Management', 'Ведение сделок от первой встречи до подписания контракта.', 'sales', 102),
  ('deal-management', 'Deal Management', 'Прогресс по стадиям воронки, работа с возражениями, stakeholder-карты.', 'pipeline-management', 201),
  ('forecasting', 'Sales Forecasting', 'Точность прогнозов, pipeline hygiene, commit vs. best case.', 'pipeline-management', 202),
  ('crm-management', 'CRM Management', 'Дисциплина ведения CRM, чистота данных, автоматизация записей.', 'pipeline-management', 203),
  ('sales-enablement', 'Sales Enablement', 'Всё, что помогает продавцам продавать быстрее и с более высоким win rate.', 'sales', 103),
  ('sales-content', 'Sales Content', 'Deck, battle cards, case studies, ROI-калькуляторы, demo-материалы.', 'sales-enablement', 201),
  ('playbooks-training', 'Playbooks & Training', 'Сценарии разговоров, objection handling, onboarding продавцов.', 'sales-enablement', 202),
  ('sales-tools', 'Sales Tools & Stack', 'Инструменты продавца — диалер, email-трекинг, intent-data, AI-копилоты.', 'sales-enablement', 203),
  ('growth-marketing', 'Growth Marketing', 'Конверсия, удержание и механики роста — эксперименты и lifecycle marketing.', NULL, 4),
  ('conversion-optimization', 'Conversion Optimization', 'CRO, A/B тесты, landing page оптимизация, улучшение форм и CTA.', 'growth-marketing', 101),
  ('lifecycle-marketing', 'Lifecycle & Retention', 'Onboarding, nurturing, upsell — работа с текущей пользовательской базой.', 'growth-marketing', 102),
  ('email-automation', 'Email Automation', 'Триггерные цепочки, behavioural emails, life-cycle sequences.', 'lifecycle-marketing', 201),
  ('product-led-growth', 'Product-Led Growth', 'Virality loops, freemium, self-serve онбординг и in-product activation.', 'lifecycle-marketing', 202),
  ('customer-expansion', 'Customer Expansion', 'Upsell, cross-sell, referral программы и community-led growth.', 'lifecycle-marketing', 203),
  ('analytics-attribution', 'Analytics & Attribution', 'Измерение эффективности каналов, атрибуция выручки и growth-метрики.', 'growth-marketing', 103),
  ('web-analytics', 'Web Analytics', 'GA4, heatmaps, session recording, funnel analysis, UTM-трекинг.', 'analytics-attribution', 201),
  ('revenue-attribution', 'Revenue Attribution', 'Multi-touch attribution, source tracking, CAC и ROI по каналам.', 'analytics-attribution', 202),
  ('revenue-ops', 'Revenue Operations', 'Операционный хребет GTM — данные, процессы, автоматизация и технологический стек.', NULL, 5),
  ('gtm-ops', 'GTM Operations', 'Процессы и операционная модель go-to-market функций.', 'revenue-ops', 101),
  ('lead-routing', 'Lead Routing & SLA', 'Правила маршрутизации лидов, SLA, handoff маркетинг → продажи.', 'gtm-ops', 201),
  ('territory-planning', 'Territory & Quota Planning', 'Сегментация рынка, распределение территорий, постановка планов продаж.', 'gtm-ops', 202),
  ('go-to-market-strategy', 'GTM Strategy', 'ICP, positioning, messaging, GTM-motion (PLG, sales-led, hybrid, channel).', 'gtm-ops', 203),
  ('data-intelligence', 'Data & Intelligence', 'Качество данных, enrichment, revenue intelligence и market insights.', 'revenue-ops', 102),
  ('data-enrichment', 'Data Enrichment', 'Обогащение контактов и аккаунтов — technographic, firmographic, intent.', 'data-intelligence', 201),
  ('revenue-intelligence', 'Revenue Intelligence', 'Conversation intelligence, deal scoring, pipeline risk AI.', 'data-intelligence', 202),
  ('market-intelligence', 'Market Intelligence', 'Конкурентный анализ, ICP-исследование, trends и win/loss анализ.', 'data-intelligence', 203),
  ('tech-stack', 'Tech Stack & Automation', 'Маркетинговый и продажный стек — инструменты, интеграции, AI-автоматизация.', 'revenue-ops', 103),
  ('crm-platform', 'CRM Platform', 'Выбор, настройка и оптимизация CRM (Salesforce, HubSpot, Pipedrive и др.).', 'tech-stack', 201),
  ('marketing-automation', 'Marketing Automation', 'MAP-системы, workflow automation, lead scoring, nurture-программы.', 'tech-stack', 202),
  ('ai-automation', 'AI & Workflow Automation', 'AI-агенты, no-code автоматизации и интеграции в GTM-процессах.', 'tech-stack', 203),
  ('ai-engineering', 'AI Engineering', 'Знания для строителей AI-систем — архитектура агентов, память, рынок.', NULL, 6),
  ('agent-engineering', 'Agent Engineering', 'Скаффолдинг, скиллы, оркестрация, паттерны доверия, MCP, LLM-as-Judge.', 'ai-engineering', 101),
  ('agent-architecture', 'Agent Architecture', 'Структурные паттерны агентных систем — scaffold, plugin, skill, multi-agent.', 'agent-engineering', 201),
  ('agent-safety', 'Agent Safety & Control', 'Trust spectrum, LLM-as-Judge, correlated judgment, human oversight.', 'agent-engineering', 202),
  ('mcp-integrations', 'MCP & Integrations', 'Model Context Protocol, MCP-серверы, tool use, agent-to-tool connectivity.', 'agent-engineering', 203),
  ('knowledge-memory', 'Knowledge & Memory', 'Память агентов, RAG, retrieval-паттерны, knowledge graphs, контекст.', 'ai-engineering', 102),
  ('rag-retrieval', 'RAG & Retrieval', 'Retrieval-Augmented Generation, chunking, semantic search, vector stores.', 'knowledge-memory', 201),
  ('memory-systems', 'Memory Systems', 'Персистентная память агентов, memory primitives, rediscovery tax.', 'knowledge-memory', 202),
  ('knowledge-graph', 'Knowledge Graphs', 'Граф-ориентированный контекст для агентов, онтологии, structured retrieval.', 'knowledge-memory', 203),
  ('ai-market', 'AI Market & Strategy', 'Рыночная динамика AI, platform risk, implementation layer, бизнес-модели.', 'ai-engineering', 103),
  ('ai-business-models', 'AI Business Models', 'SaaS commoditization, platform risk, vendor lock-in, build-vs-buy.', 'ai-market', 201),
  ('ai-infrastructure', 'AI Infrastructure Race', 'Смещение конкуренции с моделей на инфраструктуру, implementation layer.', 'ai-market', 202),
  ('ai-product-strategy', 'AI Product Strategy', 'Как строить дифференцированный AI-продукт в эпоху commodity-моделей.', 'ai-market', 203);

CREATE TEMP TABLE domain_term_aliases (
  old_key text PRIMARY KEY,
  new_key text NOT NULL
) ON COMMIT DROP;

INSERT INTO domain_term_aliases (old_key, new_key)
VALUES
  ('ai_engineering', 'ai-engineering'),
  ('agent_architecture', 'agent-architecture'),
  ('memory_rag', 'knowledge-memory'),
  ('ai_market', 'ai-market');

UPDATE kb_taxonomy_axes
SET is_hierarchical = true,
    label = 'Domain',
    description = COALESCE(description, 'RevOps and AI knowledge domain tree')
WHERE key = 'domain';

INSERT INTO kb_taxonomy_terms (axis_id, parent_id, key, label, description, weight, is_active)
SELECT
  axis.id,
  NULL,
  desired.key,
  desired.label,
  desired.description,
  desired.weight,
  true
FROM desired_domain_terms desired
JOIN kb_taxonomy_axes axis ON axis.key = 'domain'
WHERE NOT EXISTS (
  SELECT 1
  FROM kb_taxonomy_terms existing
  WHERE existing.axis_id = axis.id
    AND existing.key = desired.key
);

UPDATE kb_taxonomy_terms term
SET label = desired.label,
    description = desired.description,
    weight = desired.weight,
    is_active = true
FROM desired_domain_terms desired
JOIN kb_taxonomy_axes axis ON axis.key = 'domain'
WHERE term.axis_id = axis.id
  AND term.key = desired.key;

UPDATE kb_taxonomy_terms child
SET parent_id = parent.id
FROM desired_domain_terms desired
JOIN kb_taxonomy_axes axis ON axis.key = 'domain'
LEFT JOIN kb_taxonomy_terms parent
  ON parent.axis_id = axis.id
 AND parent.key = desired.parent_key
WHERE child.axis_id = axis.id
  AND child.key = desired.key;

INSERT INTO kb_entity_taxonomy (entity_id, term_id)
SELECT DISTINCT existing_link.entity_id, new_term.id
FROM domain_term_aliases alias
JOIN kb_taxonomy_axes axis ON axis.key = 'domain'
JOIN kb_taxonomy_terms old_term
  ON old_term.axis_id = axis.id
 AND old_term.key = alias.old_key
JOIN kb_taxonomy_terms new_term
  ON new_term.axis_id = axis.id
 AND new_term.key = alias.new_key
JOIN kb_entity_taxonomy existing_link ON existing_link.term_id = old_term.id
WHERE NOT EXISTS (
  SELECT 1
  FROM kb_entity_taxonomy duplicate
  WHERE duplicate.entity_id = existing_link.entity_id
    AND duplicate.term_id = new_term.id
);

DELETE FROM kb_entity_taxonomy link
USING domain_term_aliases alias
JOIN kb_taxonomy_axes axis ON axis.key = 'domain'
JOIN kb_taxonomy_terms old_term
  ON old_term.axis_id = axis.id
 AND old_term.key = alias.old_key
WHERE link.term_id = old_term.id;

UPDATE kb_taxonomy_terms term
SET is_active = false,
    parent_id = NULL
FROM kb_taxonomy_axes axis
WHERE term.axis_id = axis.id
  AND axis.key = 'domain'
  AND NOT EXISTS (
    SELECT 1
    FROM desired_domain_terms desired
    WHERE desired.key = term.key
  );

COMMIT;
