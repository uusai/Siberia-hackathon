-- ============================
-- ФАКУЛЬТЕТЫ
-- ============================
INSERT INTO faculties (id, name) VALUES
(1, 'Институт математики и информационных технологий'),
(2, 'Юридический институт'),
(3, 'Экономический факультет');

-- ============================
-- УЧЕБНЫЕ ПЛАНЫ
-- ============================
INSERT INTO curricula (id, program_name, faculty_id, degree_type) VALUES
(1, 'Информационная безопасность автоматизированных систем', 1, 'бакалавриат'),
(2, 'Прикладная информатика', 1, 'бакалавриат'),
(3, 'Юриспруденция', 2, 'бакалавриат'),
(4, 'Экономика предприятия', 3, 'бакалавриат'),
(5, 'Информационная безопасность', 1, 'магистратура');

-- ============================
-- УЧЕБНЫЕ ГРУППЫ
-- ============================
INSERT INTO groups (id, name, curriculum_id, year_of_study) VALUES
(1, 'БАС-24-1', 1, 1),
(2, 'БАС-23-1', 1, 2),
(3, 'ПИ-24-1', 2, 1),
(4, 'ЮР-23-2', 3, 2),
(5, 'ЭК-22-1', 4, 3);

-- ============================
-- ПРЕПОДАВАТЕЛИ
-- ============================
INSERT INTO teachers (id, full_name, faculty_id, position, academic_degree, email) VALUES
(1, 'Соколов Андрей Викторович', 1, 'профессор', 'д.т.н.', 'sokolov@igu.example'),
(2, 'Волкова Марина Сергеевна', 1, 'доцент', 'к.т.н.', 'volkova@igu.example'),
(3, 'Кузнецов Павел Игоревич', 1, 'старший преподаватель', NULL, 'kuznetsov@igu.example'),
(4, 'Морозова Елена Дмитриевна', 2, 'доцент', 'к.ю.н.', 'morozova@igu.example'),
(5, 'Лебедев Игорь Николаевич', 3, 'профессор', 'д.э.н.', 'lebedev@igu.example'),
(6, 'Новикова Ольга Александровна', 1, 'ассистент', NULL, 'novikova@igu.example');

-- ============================
-- АДМИНИСТРАЦИЯ
-- ============================
INSERT INTO administration (id, full_name, position, faculty_id, email) VALUES
(1, 'Титов Сергей Леонидович', 'ректор', NULL, 'rector@igu.example'),
(2, 'Захарова Наталья Петровна', 'декан', 1, 'dean.it@igu.example'),
(3, 'Егоров Дмитрий Олегович', 'декан', 2, 'dean.law@igu.example'),
(4, 'Белова Анна Сергеевна', 'начальник приёмной комиссии', NULL, 'admissions@igu.example');

-- ============================
-- СТУДЕНТЫ
-- ============================
INSERT INTO students (id, full_name, group_id, status, enrollment_year) VALUES
(1, 'Иванов Тимофей Алексеевич', 1, 'active', 2024),
(2, 'Смирнова Дарья Витальевна', 1, 'active', 2024),
(3, 'Петров Артём Юрьевич', 1, 'academic_leave', 2024),
(4, 'Васильева Ксения Романовна', 2, 'active', 2023),
(5, 'Фёдоров Никита Сергеевич', 2, 'active', 2023),
(6, 'Орлова Полина Игоревна', 3, 'active', 2024),
(7, 'Григорьев Максим Олегович', 4, 'expelled', 2023),
(8, 'Романова Виктория Андреевна', 5, 'graduated', 2022);

-- ============================
-- АБИТУРИЕНТЫ
-- ============================
INSERT INTO applicants (id, full_name, curriculum_id, exam_score, status, application_year) VALUES
(1, 'Ковалёв Данила Сергеевич', 1, 267, 'enrolled', 2026),
(2, 'Соловьёва Алина Дмитриевна', 1, 254, 'enrolled', 2026),
(3, 'Медведев Артур Викторович', 1, 198, 'rejected', 2026),
(4, 'Никитина Софья Андреевна', 2, 231, 'enrolled', 2026),
(5, 'Захаров Илья Максимович', 3, 245, 'applied', 2026),
(6, 'Павлова Мария Игоревна', 4, 210, 'applied', 2026),
(7, 'Семёнов Глеб Николаевич', 1, 189, 'rejected', 2026);

-- ============================
-- АГРЕГИРОВАННАЯ СТАТИСТИКА ПРИЁМА
-- ============================
INSERT INTO admissions_stats (id, year, faculty_id, applied_count, enrolled_count, avg_exam_score) VALUES
(1, 2026, 1, 120, 45, 231.40),
(2, 2026, 2, 80, 30, 220.10),
(3, 2026, 3, 60, 25, 205.75),
(4, 2025, 1, 110, 42, 227.90);

-- ============================
-- ПОЛЬЗОВАТЕЛИ СИСТЕМЫ (пароли — bcrypt-хэши от "test123", замени на свои)
-- ============================
INSERT INTO users (id, username, password_hash, role) VALUES
(1, 'admin1', '$2b$12$replace_with_real_bcrypt_hash', 'admin'),
(2, 'dean_it', '$2b$12$replace_with_real_bcrypt_hash', 'teacher'),
(3, 'admissions1', '$2b$12$replace_with_real_bcrypt_hash', 'admissions_staff'),
(4, 'guest1', '$2b$12$replace_with_real_bcrypt_hash', 'guest');

-- сброс автоинкрементов после ручной вставки id
SELECT setval('faculties_id_seq', (SELECT MAX(id) FROM faculties));
SELECT setval('curricula_id_seq', (SELECT MAX(id) FROM curricula));
SELECT setval('groups_id_seq', (SELECT MAX(id) FROM groups));
SELECT setval('teachers_id_seq', (SELECT MAX(id) FROM teachers));
SELECT setval('administration_id_seq', (SELECT MAX(id) FROM administration));
SELECT setval('students_id_seq', (SELECT MAX(id) FROM students));
SELECT setval('applicants_id_seq', (SELECT MAX(id) FROM applicants));
SELECT setval('admissions_stats_id_seq', (SELECT MAX(id) FROM admissions_stats));
SELECT setval('users_id_seq', (SELECT MAX(id) FROM users));