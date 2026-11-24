-- RankForge demo data. Run after schema.sql (safe to re-run).
-- Rankings and clicks below are SIMULATED demo data (source = 'simulated').
-- Commission rates are illustrative, based on publicly listed program ranges.

-- ---------- Demo users ----------
insert into users (user_id, display_name, is_admin) values
  ('demo_alice', 'Alice — TrailReady (hiking gear blog)', false),
  ('demo_bob',   'Bob — BrewBench (home coffee blog)',    false),
  ('admin',      'RankForge Admin',                       true)
on conflict (user_id) do nothing;

-- ---------- Long-term memory for demo users ----------
insert into user_memory (user_id, key, value) values
  ('demo_alice', 'website',   'TrailReady'),
  ('demo_alice', 'niche',     'budget hiking and backpacking gear'),
  ('demo_alice', 'tone',      'friendly, practical, no hype'),
  ('demo_alice', 'audience',  'beginner hikers on a budget'),
  ('demo_alice', 'location',  'United States'),
  ('demo_alice', 'content_preferences', 'prefers REI Co-op and Amazon Associates links'),
  ('demo_bob',   'website',   'BrewBench'),
  ('demo_bob',   'niche',     'home espresso and coffee gear'),
  ('demo_bob',   'tone',      'nerdy, detailed, data-driven'),
  ('demo_bob',   'audience',  'home baristas upgrading their setup'),
  ('demo_bob',   'location',  'United States'),
  ('demo_bob',   'content_preferences', 'prefers Amazon Associates and Seattle Coffee Gear links')
on conflict (user_id, key) do nothing;

-- ---------- Affiliate catalog ----------
insert into affiliate_programs (name, network, category, commission_pct, base_url) values
  ('Amazon — Outdoor Recreation', 'Amazon Associates', 'outdoors',    3.0, 'https://www.amazon.com/s?k={q}'),
  ('Amazon — Kitchen',            'Amazon Associates', 'kitchen',     4.5, 'https://www.amazon.com/s?k={q}'),
  ('Amazon — Electronics',        'Amazon Associates', 'electronics', 3.0, 'https://www.amazon.com/s?k={q}'),
  ('Amazon — Home',               'Amazon Associates', 'home',        3.0, 'https://www.amazon.com/s?k={q}'),
  ('Amazon — Books',              'Amazon Associates', 'books',       4.5, 'https://www.amazon.com/s?k={q}'),
  ('REI Co-op',                   'AvantLink',         'outdoors',    5.0, 'https://www.rei.com/search?q={q}'),
  ('Backcountry',                 'AvantLink',         'outdoors',    8.0, 'https://www.backcountry.com/search?q={q}'),
  ('Moosejaw',                    'AvantLink',         'outdoors',    6.0, 'https://www.moosejaw.com/search?q={q}'),
  ('Seattle Coffee Gear',         'Impact',            'coffee',      6.0, 'https://www.seattlecoffeegear.com/search?q={q}'),
  ('Trade Coffee',                'Impact',            'coffee',     10.0, 'https://www.drinktrade.com/search?q={q}'),
  ('Best Buy',                    'Impact',            'electronics', 1.0, 'https://www.bestbuy.com/site/searchpage.jsp?st={q}'),
  ('Bluehost',                    'Impact',            'software',   40.0, 'https://www.bluehost.com/'),
  ('NordVPN',                     'CJ',                'software',   40.0, 'https://nordvpn.com/'),
  ('Grammarly',                   'Impact',            'software',   20.0, 'https://www.grammarly.com/'),
  ('Wayfair',                     'CJ',                'home',        5.0, 'https://www.wayfair.com/keyword.php?keyword={q}'),
  ('Chewy',                       'CJ',                'pets',        4.0, 'https://www.chewy.com/s?query={q}'),
  ('Etsy',                        'Awin',              'crafts',      4.0, 'https://www.etsy.com/search?q={q}'),
  ('Sephora',                     'Rakuten',           'beauty',      5.0, 'https://www.sephora.com/search?keyword={q}'),
  ('Udemy',                       'Rakuten',           'education',  15.0, 'https://www.udemy.com/courses/search/?q={q}'),
  ('Garmin',                      'CJ',                'electronics', 4.0, 'https://www.garmin.com/en-US/search/?query={q}'),
  ('Amazon — Sports & Fitness',   'Amazon Associates', 'fitness',     3.0, 'https://www.amazon.com/s?k={q}'),
  ('Myprotein',                   'Awin',              'fitness',     8.0, 'https://www.myprotein.com/search/?search={q}'),
  ('Lenovo',                      'Impact',            'electronics', 3.0, 'https://www.lenovo.com/us/en/search?text={q}')
on conflict (name) do nothing;

-- ---------- Demo posts ----------
insert into posts (user_id, slug, title, keyword, meta_description, html, status) values
  ('demo_alice', 'best-budget-hiking-boots',
   'Best Budget Hiking Boots (Tested on Real Trails)', 'best budget hiking boots',
   'Our picks for the best budget hiking boots under $120, tested for comfort, grip and durability.',
   '<h1>Best Budget Hiking Boots</h1><p>Demo article body.</p>', 'published'),
  ('demo_alice', 'ultralight-tent-under-200',
   'Ultralight Tents Under $200: What Actually Works', 'ultralight tent under 200',
   'Five ultralight tents under $200 compared on weight, packed size and weather protection.',
   '<h1>Ultralight Tents Under $200</h1><p>Demo article body.</p>', 'published'),
  ('demo_alice', 'beginner-day-hike-checklist',
   'The Beginner Day Hike Checklist', 'day hike checklist',
   'Everything a beginner needs for a safe day hike, with budget gear picks.',
   '<h1>Beginner Day Hike Checklist</h1><p>Demo article body.</p>', 'published'),
  ('demo_bob', 'best-espresso-machine-under-500',
   'Best Espresso Machines Under $500', 'best espresso machine under 500',
   'We compare the best home espresso machines under $500 on shot quality, steam power and ease of use.',
   '<h1>Best Espresso Machines Under $500</h1><p>Demo article body.</p>', 'published'),
  ('demo_bob', 'burr-grinder-vs-blade-grinder',
   'Burr Grinder vs Blade Grinder: Does It Matter?', 'burr grinder vs blade grinder',
   'Why grind consistency matters, and which burr grinders are worth the upgrade.',
   '<h1>Burr vs Blade Grinder</h1><p>Demo article body.</p>', 'published')
on conflict (slug) do nothing;

-- ---------- Simulated 30-day ranking history ----------
-- Each post gets a trend: improving, flat, or declining (so the Optimizer has signals).
insert into rankings (user_id, post_id, keyword, position, source, date)
select p.user_id, p.id, p.keyword,
       greatest(1, least(100,
         case p.slug
           when 'best-budget-hiking-boots'        then 40 - d            -- improving: 40 -> 11
           when 'ultralight-tent-under-200'       then 18 + (d % 3)      -- flat ~18-20
           when 'beginner-day-hike-checklist'     then 25 + d            -- declining: 25 -> 54
           when 'best-espresso-machine-under-500' then 12 - (d / 5)      -- improving: 12 -> 7
           else 35 + (d / 2)                                             -- declining slowly
         end + ((d * 7) % 3) - 1)),
       'simulated',
       current_date - (29 - d)
from posts p
cross join generate_series(0, 29) as d
where p.user_id in ('demo_alice', 'demo_bob')
on conflict (post_id, keyword, date) do nothing;

-- ---------- Simulated 30-day click history ----------
insert into clicks (user_id, post_id, date, sessions, affiliate_clicks, conversions)
select p.user_id, p.id, current_date - (29 - d),
       s.sessions,
       (s.sessions * 0.08)::int,
       (s.sessions * 0.08 * 0.05)::int
from posts p
cross join generate_series(0, 29) as d
cross join lateral (
  select greatest(0,
    case p.slug
      when 'best-budget-hiking-boots'        then 20 + d * 4
      when 'ultralight-tent-under-200'       then 60 + (d % 5) * 3
      when 'beginner-day-hike-checklist'     then 90 - d * 2
      when 'best-espresso-machine-under-500' then 120 + d * 3
      else 40 - d
    end) as sessions
) s
where p.user_id in ('demo_alice', 'demo_bob')
on conflict (post_id, date) do nothing;
