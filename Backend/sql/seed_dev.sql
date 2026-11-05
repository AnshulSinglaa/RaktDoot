-- seed_dev.sql : DEV ONLY sample data. Never run against anything shared.
INSERT INTO organizations (kind, name, city) VALUES
    ('blood_bank', 'City Blood Bank',   'Bhopal'),
    ('blood_bank', 'Red Cross Centre',  'Indore'),
    ('blood_bank', 'New Empty Bank',    'Raipur'),
    ('hospital',   'AIIMS Bhopal',      'Bhopal');

INSERT INTO donors (name, email, age, blood_group, last_donated_at) VALUES
    ('Asha',  'asha@example.com',  30, 'O+', now() - interval '120 days'),
    ('Ravi',  'ravi@example.com',  41, 'A+', now() - interval '20 days'),
    ('Meera', 'meera@example.com', 27, 'O-', NULL);

INSERT INTO blood_units (blood_bank_id, blood_group, collected_at, expires_at)
SELECT o.id, t.grp, now() - interval '5 days', now() + (t.days || ' days')::interval
FROM organizations o
JOIN (VALUES
    ('City Blood Bank',  'O+', 20),
    ('City Blood Bank',  'O+', 30),
    ('City Blood Bank',  'A+', 25),
    ('City Blood Bank',  'O-', 28),
    ('Red Cross Centre', 'O+', 22),
    ('Red Cross Centre', 'B+', 18)
) AS t(bank, grp, days) ON t.bank = o.name;