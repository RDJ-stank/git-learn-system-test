# app.py
import webbrowser
from threading import Timer

from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, abort
from config import Config
from models import db, User, Course, Chapter, LearningBehavior, Question, WrongQuestion, Note
from datetime import datetime, timedelta
import json
from openai import OpenAI
import time
import os
import pandas as pd
import numpy as np
from sklearn.cluster import KMeans


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    db.init_app(app)

    @app.route('/')
    def index():
        if 'user_id' not in session: return redirect(url_for('login'))
        if session.get('role') == 'teacher': return redirect(url_for('admin_dashboard'))

        user_id = session['user_id']
        current_user = db.session.get(User, user_id)
        courses = Course.query.all()

        course_progress = {}
        for c in courses:
            total_chapters = Chapter.query.filter_by(course_id=c.id).count()
            if total_chapters == 0:
                course_progress[c.id] = 0
            else:
                watched = db.session.query(LearningBehavior.chapter_id).filter(
                    LearningBehavior.user_id == user_id,
                    LearningBehavior.course_id == c.id,
                    LearningBehavior.action_type == 'watch_video',
                    LearningBehavior.chapter_id.isnot(None)
                ).distinct().count()
                progress = int((watched / total_chapters) * 100)
                course_progress[c.id] = min(progress, 100)

        today = datetime.now().date()
        today_behaviors = LearningBehavior.query.filter(
            LearningBehavior.user_id == user_id,
            db.func.date(LearningBehavior.timestamp) == today
        ).all()
        today_duration = sum([b.duration for b in today_behaviors if b.duration]) // 60

        mistakes = WrongQuestion.query.filter_by(user_id=user_id).all()
        total_mistakes = 0
        for m in mistakes:
            if Question.query.filter_by(content=m.question_content).first():
                total_mistakes += 1
            else:
                db.session.delete(m)
        db.session.commit()

        last_interaction = LearningBehavior.query.filter(
            LearningBehavior.user_id == user_id,
            LearningBehavior.chapter_id.isnot(None)
        ).order_by(LearningBehavior.timestamp.desc()).first()

        last_studied_chapter = None
        last_studied_course = None
        if last_interaction:
            last_studied_chapter = db.session.get(Chapter, last_interaction.chapter_id)
            if last_studied_chapter:
                last_studied_course = last_studied_chapter.course

        recent_behaviors = LearningBehavior.query.filter_by(user_id=user_id).order_by(
            LearningBehavior.timestamp.desc()).limit(8).all()
        footprints = []
        for b in recent_behaviors:
            action_text = ""
            course_title = ""
            if b.course_id:
                course_obj = db.session.get(Course, b.course_id)
                course_title = course_obj.title if course_obj else "未知课程"

            if b.action_type == 'login':
                action_text = "登录了智能学习系统"
                icon, color = "bi-box-arrow-in-right", "secondary"
            elif b.action_type == 'visit_course':
                action_text = f"访问了课程《{course_title}》"
                icon, color = "bi-book", "primary"
            elif b.action_type == 'watch_video':
                mins = b.duration // 60
                secs = b.duration % 60
                time_str = f"{mins}分{secs}秒" if mins > 0 else f"{secs}秒"
                action_text = f"观看了《{course_title}》 (时长: {time_str})"
                icon, color = "bi-play-circle", "success"
            elif b.action_type == 'quiz':
                action_text = f"完成了《{course_title}》的测验"
                icon, color = "bi-pencil-square", "warning"

            footprints.append({
                'time': b.timestamp.strftime('%m-%d %H:%M'),
                'text': action_text,
                'icon': icon,
                'color': color
            })

        if not last_studied_course and courses:
            last_studied_course = courses[0]
            last_studied_chapter = Chapter.query.filter_by(course_id=last_studied_course.id).first()

        one_week_ago = today - timedelta(days=7)
        weekly_behaviors = LearningBehavior.query.filter(
            LearningBehavior.user_id == user_id,
            db.func.date(LearningBehavior.timestamp) >= one_week_ago
        ).all()
        weekly_duration = sum([b.duration for b in weekly_behaviors if b.duration]) // 60
        weekly_goal = current_user.weekly_goal if current_user.weekly_goal else 120
        weekly_progress = min(int((weekly_duration / weekly_goal) * 100), 100) if weekly_goal > 0 else 0

        recent_notes = Note.query.filter_by(user_id=user_id).order_by(Note.timestamp.desc()).limit(4).all()
        recent_notes_data = []
        for note in recent_notes:
            if not note.content.strip(): continue
            chapter = db.session.get(Chapter, note.chapter_id)
            course_name = chapter.course.title if chapter and chapter.course else "未知课程"
            recent_notes_data.append({
                'course_name': course_name,
                'content': note.content[:60] + '...' if len(note.content) > 60 else note.content,
                'time': note.timestamp.strftime('%m-%d %H:%M')
            })

        return render_template('index.html',
                               username=session.get('username'),
                               courses=courses,
                               course_progress=course_progress,
                               today_duration=today_duration,
                               total_mistakes=total_mistakes,
                               footprints=footprints,
                               last_studied_course=last_studied_course,
                               last_studied_chapter=last_studied_chapter,
                               weekly_duration=weekly_duration,
                               weekly_goal=weekly_goal,
                               weekly_progress=weekly_progress,
                               recent_notes_data=recent_notes_data)

    @app.route('/update_weekly_goal', methods=['POST'])
    def update_weekly_goal():
        if 'user_id' not in session: return jsonify({'status': 'error'})
        data = request.get_json()
        new_goal = data.get('goal', 120)
        user = db.session.get(User, session['user_id'])
        if user:
            user.weekly_goal = int(new_goal)
            db.session.commit()
            return jsonify({'status': 'success'})
        return jsonify({'status': 'error'})

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            username = request.form.get('username')
            password = request.form.get('password')
            action = request.form.get('action')
            if action == 'register':
                if not User.query.filter_by(username=username).first():
                    db.session.add(User(username=username, password=password, role='student'))
                    db.session.commit()
                    flash('注册成功')
            elif action == 'login':
                user = User.query.filter_by(username=username, password=password).first()
                if user:
                    session['user_id'] = user.id
                    session['username'] = user.username
                    session['role'] = user.role
                    if user.role == 'student':
                        db.session.add(LearningBehavior(user_id=user.id, action_type='login', duration=0))
                        db.session.commit()
                    return redirect(url_for('admin_dashboard')) if user.role == 'teacher' else redirect(
                        url_for('index'))
        return render_template('login.html')

    @app.route('/logout')
    def logout():
        session.clear()
        return redirect(url_for('login'))

    @app.route('/admin')
    def admin_dashboard():
        if 'user_id' not in session or session.get('role') != 'teacher': return redirect(url_for('login'))
        students = User.query.filter_by(role='student').all()
        courses = Course.query.all()

        filter_course_id = request.args.get('course_id', 'all')

        course_stats = []
        for c in courses:
            dur = db.session.query(db.func.sum(LearningBehavior.duration)).filter_by(course_id=c.id).scalar() or 0
            course_stats.append({'name': c.title, 'value': int(dur) // 60})

        student_data = []
        for s in students:
            if filter_course_id != 'all':
                cid = int(filter_course_id)
                total = db.session.query(db.func.sum(LearningBehavior.duration)).filter_by(user_id=s.id,
                                                                                           course_id=cid).scalar() or 0
                mistakes = db.session.query(WrongQuestion).join(Question,
                                                                WrongQuestion.question_content == Question.content).filter(
                    WrongQuestion.user_id == s.id, Question.course_id == cid).count()
                activity_count = LearningBehavior.query.filter_by(user_id=s.id, course_id=cid).count()
            else:
                total = db.session.query(db.func.sum(LearningBehavior.duration)).filter_by(user_id=s.id).scalar() or 0
                mistakes = WrongQuestion.query.filter_by(user_id=s.id).count()
                activity_count = LearningBehavior.query.filter_by(user_id=s.id).count()

            total_min = int(total) // 60
            student_data.append({'id': s.id, 'name': s.username, 'duration': total_min, 'mistakes': mistakes,
                                 'activity': activity_count})

        df = pd.DataFrame(student_data)
        analysis_results = []
        can_cluster = False
        if len(df) >= 3:
            if len(df[['duration', 'mistakes', 'activity']].drop_duplicates()) >= 3:
                can_cluster = True

        if can_cluster:
            try:
                X = df[['duration', 'mistakes', 'activity']]
                kmeans = KMeans(n_clusters=3, random_state=42, n_init=10)
                df['cluster'] = kmeans.fit_predict(X)
                cluster_means = df.groupby('cluster')['duration'].mean().sort_values()
                sorted_clusters = cluster_means.index.tolist()

                if len(sorted_clusters) < 3: raise ValueError("Clusters less than 3")

                label_map = {
                    sorted_clusters[0]: {'text': '⚠️ 需关注', 'color': 'danger'},
                    sorted_clusters[1]: {'text': '🌱 发展中', 'color': 'warning'},
                    sorted_clusters[2]: {'text': '🌟 积极分子', 'color': 'success'}
                }

                for index, row in df.iterrows():
                    cluster_info = label_map[row['cluster']]
                    analysis_results.append({
                        'id': row['id'], 'name': row['name'], 'duration': row['duration'],
                        'mistakes': row['mistakes'], 'label': cluster_info['text'], 'color': cluster_info['color']
                    })
            except Exception as e:
                for s in student_data:
                    analysis_results.append(
                        {'id': s['id'], 'name': s['name'], 'duration': s['duration'], 'mistakes': s['mistakes'],
                         'label': '数据不足', 'color': 'secondary'})
        else:
            for s in student_data:
                analysis_results.append(
                    {'id': s['id'], 'name': s['name'], 'duration': s['duration'], 'mistakes': s['mistakes'],
                     'label': '数据不足', 'color': 'secondary'})

        return render_template('admin.html', username=session.get('username'), students=analysis_results,
                               courses=courses, course_stats=course_stats, filter_course_id=filter_course_id)

    @app.route('/api/admin/evaluate_student/<int:student_id>', methods=['POST'])
    def evaluate_student(student_id):
        if 'user_id' not in session or session.get('role') != 'teacher': return jsonify(
            {'status': 'error', 'msg': '无权限'})

        student = db.session.get(User, student_id)
        if not student: return jsonify({'status': 'error', 'msg': '用户不存在'})

        data = request.get_json()
        filter_course_id = data.get('course_id', 'all')

        if filter_course_id != 'all':
            cid = int(filter_course_id)
            course_obj = db.session.get(Course, cid)
            course_title = course_obj.title if course_obj else "该课程"
            total_sec = db.session.query(db.func.sum(LearningBehavior.duration)).filter_by(user_id=student.id,
                                                                                           course_id=cid).scalar() or 0
            mistakes = db.session.query(WrongQuestion).join(Question,
                                                            WrongQuestion.question_content == Question.content).filter(
                WrongQuestion.user_id == student.id, Question.course_id == cid).count()
            context_str = f"针对《{course_title}》这门课程"
        else:
            total_sec = db.session.query(db.func.sum(LearningBehavior.duration)).filter_by(
                user_id=student.id).scalar() or 0
            mistakes = WrongQuestion.query.filter_by(user_id=student.id).count()
            context_str = "针对其全局学习情况"

        total_min = int(total_sec) // 60

        prompt = f"""
        你是一名资深的教研主管。请对学生【{student.username}】出具一份简短的个体评估报告。
        数据前提：{context_str}，该学生累计专注学习时长为 {total_min} 分钟，当前滞留待解决错题数为 {mistakes} 题。
        请结合上述数据，评估其学习态度、存在的问题，并给出具体的督促或辅导建议。
        要求：字数150字左右，直接输出清晰的纯文本，严禁使用任何Markdown符号或星号。
        """
        try:
            client = OpenAI(api_key="sk-3Olyx8Ft9DJOGVcAAbfpy8FT78UvncXOoJAdLOjRVlgrOj0h",
                            base_url="https://api.moonshot.cn/v1")
            response = client.chat.completions.create(
                model="moonshot-v1-8k",
                messages=[{"role": "system", "content": "你是教研主管"}, {"role": "user", "content": prompt}],
                temperature=0.7
            )
            advice = response.choices[0].message.content.replace('\n', '<br>')
            return jsonify({'status': 'success', 'analysis': advice})
        except Exception as e:
            return jsonify({'status': 'error', 'msg': 'AI 评估接口超时，请重试。'})

    @app.route('/admin/course/<int:course_id>')
    def admin_course_edit(course_id):
        if 'user_id' not in session or session.get('role') != 'teacher': return redirect(url_for('login'))
        course = db.session.get(Course, course_id)
        if not course: abort(404)
        chapters = Chapter.query.filter_by(course_id=course_id).all()
        open_chapter = request.args.get('open_chapter', type=int)
        return render_template('admin_course_edit.html', course=course, chapters=chapters, open_chapter=open_chapter)

    @app.route('/admin/course/add_chapter/<int:course_id>', methods=['POST'])
    def add_chapter(course_id):
        if 'user_id' not in session or session.get('role') != 'teacher': return redirect(url_for('login'))
        chapter_title = request.form.get('chapter_title')
        content_summary = request.form.get('content_summary')
        video_file = request.files.get('video_file')
        video_url = ""
        if video_file:
            file_ext = video_file.filename.split('.')[-1]
            new_filename = f"c{course_id}_{int(time.time())}.{file_ext}"
            save_path = os.path.join(app.root_path, 'static', 'videos', new_filename)
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            video_file.save(save_path)
            video_url = f"videos/{new_filename}"

        new_chapter = Chapter(course_id=course_id, title=chapter_title, video_url=video_url,
                              content_summary=content_summary)
        db.session.add(new_chapter)
        db.session.commit()
        flash('✅ 新章节及讲义添加成功！')
        return redirect(url_for('admin_course_edit', course_id=course_id))

    @app.route('/admin/chapter/update/<int:chapter_id>', methods=['POST'])
    def update_chapter(chapter_id):
        if 'user_id' not in session or session.get('role') != 'teacher': return redirect(url_for('login'))
        chapter = db.session.get(Chapter, chapter_id)
        if not chapter: abort(404)
        new_title = request.form.get('title')
        if new_title: chapter.title = new_title

        chapter.content_summary = request.form.get('content_summary')

        video_file = request.files.get('video_file')
        if video_file:
            file_ext = video_file.filename.split('.')[-1]
            new_filename = f"c{chapter.course_id}_{int(time.time())}.{file_ext}"
            save_path = os.path.join(app.root_path, 'static', 'videos', new_filename)
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            video_file.save(save_path)
            chapter.video_url = f"videos/{new_filename}"

        db.session.commit()
        flash('✅ 章节讲义及信息已更新！')
        return redirect(url_for('admin_course_edit', course_id=chapter.course_id,
                                open_chapter=chapter.id) + f'#chapter_{chapter.id}')

    @app.route('/admin/chapter/delete/<int:chapter_id>')
    def delete_chapter(chapter_id):
        if 'user_id' not in session or session.get('role') != 'teacher': return redirect(url_for('login'))
        chapter = db.session.get(Chapter, chapter_id)
        if not chapter: abort(404)
        course_id = chapter.course_id

        behaviors = LearningBehavior.query.filter_by(chapter_id=chapter_id).all()
        for b in behaviors:
            b.chapter_id = None

        Question.query.filter_by(chapter_id=chapter_id).delete()
        Note.query.filter_by(chapter_id=chapter_id).delete()

        db.session.delete(chapter)
        db.session.commit()

        flash('🗑️ 章节及其关联的题目已安全删除！')
        return redirect(url_for('admin_course_edit', course_id=course_id))

    @app.route('/admin/question/delete/<int:question_id>')
    def delete_question(question_id):
        if 'user_id' not in session or session.get('role') != 'teacher': return redirect(url_for('login'))
        q = db.session.get(Question, question_id)
        if not q: abort(404)
        course_id = q.course_id
        chapter_id = q.chapter_id
        db.session.delete(q)
        db.session.commit()
        return redirect(
            url_for('admin_course_edit', course_id=course_id, open_chapter=chapter_id) + f'#chapter_{chapter_id}')

    @app.route('/auto_generate_quiz/<int:chapter_id>', methods=['POST'])
    def auto_generate_quiz_chapter(chapter_id):
        if 'user_id' not in session or session.get('role') != 'teacher': return redirect(url_for('login'))
        chapter = db.session.get(Chapter, chapter_id)
        if not chapter: abort(404)
        course = chapter.course
        num_questions = request.form.get('num_questions', 3, type=int)

        content_text = chapter.content_summary.strip() if chapter.content_summary else ""
        if content_text:
            context_prompt = f"【本节视频核心讲义】：\n{content_text}\n请**绝对且严格**根据以上讲义内容生成题目，确保题目知识点全部在讲义范围内，绝对不能超纲。"
        else:
            context_prompt = f"【提示】：老师暂未上传视频讲义。请根据章节标题“{chapter.title}”涵盖的基础通用知识自行推断并生成题目。"

        prompt = f"""
        你是一名严谨的出题专家。请根据课程《{course.title}》中的章节《{chapter.title}》生成 {num_questions} 道测试题。

        {context_prompt}

        【核心要求】
        1. 题目类型应当随机混合【单项选择题】、【判断题】和【填空题】。
        2. 为了完美适配现有系统格式，请对不同题型做以下特殊转换：
           - 判断题：请将 A 选项固定为“正确”，B 选项固定为“错误”，C 和 D 选项固定填入“-”。
           - 填空题：请将题干中的空白处用“___”表示，并将填空的标准答案及 3 个易混淆的错误词汇作为 A, B, C, D 四个备选项（即转化为选词填空形式）。
        3. 必须针对该章节提取具体的【知识点/考点】（字数严格限制在2-6个字之间）。
        4. 必须严格且仅返回以下JSON格式数组，绝不要包含任何额外的Markdown符号或解释性文字：
        [
            {{
                "content": "题目描述（若是填空题请包含___）",
                "option_a": "选项A内容",
                "option_b": "选项B内容",
                "option_c": "选项C内容",
                "option_d": "选项D内容",
                "correct_answer": "A",
                "knowledge_point": "2-6字考点词汇"
            }}
        ]
        """
        try:
            client = OpenAI(api_key="sk-3Olyx8Ft9DJOGVcAAbfpy8FT78UvncXOoJAdLOjRVlgrOj0h",
                            base_url="https://api.moonshot.cn/v1")
            response = client.chat.completions.create(
                model="moonshot-v1-8k",
                messages=[{"role": "system", "content": "你是一个严格按照JSON格式输出的AI助手。"},
                          {"role": "user", "content": prompt}],
                temperature=0.6
            )
            ai_text = response.choices[0].message.content.replace("```json", "").replace("```", "").strip()
            try:
                questions_data = json.loads(ai_text)
            except:
                start = ai_text.find('[')
                end = ai_text.rfind(']') + 1
                questions_data = json.loads(ai_text[start:end])

            count = 0
            for q in questions_data:
                kp = q.get('knowledge_point', '').strip()
                if not kp or "填入具体" in kp or "考察的知识点" in kp or "综合考点" in kp:
                    clean_title = chapter.title.split(' ')[-1] if ' ' in chapter.title else chapter.title
                    kp = f"{clean_title[:5]}相关"

                new_q = Question(
                    course_id=course.id, chapter_id=chapter.id,
                    content=q.get('content') or "内容缺失",
                    option_a=q.get('option_a') or "A", option_b=q.get('option_b') or "B",
                    option_c=q.get('option_c') or "C", option_d=q.get('option_d') or "D",
                    correct_answer=q.get('correct_answer') or "A",
                    knowledge_point=kp
                )
                db.session.add(new_q)
                count += 1
            db.session.commit()

            if content_text:
                flash(f'⚡ 已为您生成了 {count} 道混合题型（选择/判断/填空）！')
            else:
                flash(f'⚡ 成功生成 {count} 道混合题型！(补充讲义可提升精度)')

        except Exception as e:
            print(f"AI Error: {e}")
            flash('AI出题遇到小状况，请重试')

        return redirect(
            url_for('admin_course_edit', course_id=course.id, open_chapter=chapter.id) + f'#chapter_{chapter.id}')

    @app.route('/add_course', methods=['POST'])
    def add_course():
        if 'user_id' not in session: return redirect(url_for('login'))
        title = request.form.get('title')
        c_type = request.form.get('content_type')
        new_course = Course(title=title, content_type=c_type)
        db.session.add(new_course)
        db.session.commit()
        return redirect(url_for('admin_dashboard'))

    @app.route('/admin/course/update/<int:course_id>', methods=['POST'])
    def admin_course_update(course_id):
        if 'user_id' not in session or session.get('role') != 'teacher': return redirect(url_for('login'))
        course = db.session.get(Course, course_id)
        if not course: abort(404)

        new_title = request.form.get('title')
        if new_title: course.title = new_title

        new_type = request.form.get('content_type')
        if new_type: course.content_type = new_type

        db.session.commit()
        flash('✅ 课程基本信息已成功更新！')
        return redirect(url_for('admin_course_edit', course_id=course.id))

    @app.route('/admin/course/delete/<int:course_id>')
    def delete_course(course_id):
        if 'user_id' not in session or session.get('role') != 'teacher': return redirect(url_for('login'))
        course = db.session.get(Course, course_id)
        if not course: abort(404)

        behaviors = LearningBehavior.query.filter_by(course_id=course_id).all()
        for b in behaviors:
            b.course_id = None
            b.chapter_id = None

        Question.query.filter_by(course_id=course_id).delete()
        for ch in course.chapters:
            Note.query.filter_by(chapter_id=ch.id).delete()
            db.session.delete(ch)

        db.session.delete(course)
        db.session.commit()

        flash('🗑️ 课程及其所有内容已被彻底删除！')
        return redirect(url_for('admin_dashboard'))

    @app.route('/course/<int:course_id>')
    def course_detail(course_id):
        if 'user_id' not in session: return redirect(url_for('login'))
        user_id = session['user_id']
        course = db.session.get(Course, course_id)
        if not course: abort(404)
        chapters = Chapter.query.filter_by(course_id=course_id).all()

        current_chapter_id = request.args.get('chapter_id', type=int)
        current_chapter = None
        if chapters:
            if current_chapter_id:
                current_chapter = db.session.get(Chapter, current_chapter_id)
            else:
                current_chapter = chapters[0]

        db.session.add(LearningBehavior(user_id=user_id, course_id=course.id,
                                        chapter_id=current_chapter.id if current_chapter else None,
                                        action_type='visit_course', duration=0))
        db.session.commit()

        progress = 0
        total_chapters = len(chapters)
        if total_chapters > 0:
            watched = db.session.query(LearningBehavior.chapter_id).filter(
                LearningBehavior.user_id == user_id,
                LearningBehavior.course_id == course.id,
                LearningBehavior.action_type == 'watch_video',
                LearningBehavior.chapter_id.isnot(None)
            ).distinct().count()
            progress = int((watched / total_chapters) * 100)

        return render_template('course.html', course=course, chapters=chapters, current_chapter=current_chapter,
                               progress=progress)

    # --- [核心修改] 接收动态题数并防呆 ---
    @app.route('/api/student_generate_quiz', methods=['POST'])
    def api_student_generate_quiz():
        if 'user_id' not in session: return jsonify({'status': 'error', 'msg': '未登录'})
        data = request.get_json()
        course_id = data.get('course_id')
        req_text = data.get('requirements', '生成基础巩固练习')

        # 获取前端传来的题目数量，默认3道，防呆兜底上限控制在10道内防超时
        try:
            num_questions = int(data.get('num_questions', 3))
            if num_questions < 1: num_questions = 1
            if num_questions > 10: num_questions = 10
        except ValueError:
            num_questions = 3

        course = db.session.get(Course, course_id)
        if not course: return jsonify({'status': 'error', 'msg': '参数错误，找不到该课程'})

        # 将动态数量注入Prompt
        prompt = f"""
        你是一名深谙因材施教的出题专家。学生正在自主复习《{course.title}》。
        学生针对本次专项练习提出了以下明确要求：“{req_text}”。
        请严格根据学生的该要求，为其量身定制 {num_questions} 道测试题。

        【核心要求】
        1. 题目类型应当随机混合【单项选择题】、【判断题】和【填空题】。
        2. 为了完美适配现有系统格式，请对不同题型做以下特殊转换：
           - 判断题：请将 A 选项固定为“正确”，B 选项固定为“错误”，C 和 D 选项固定填入“-”。
           - 填空题：请将题干中的空白处用“___”表示，并将填空的标准答案及 3 个易混淆的错误词汇作为 A, B, C, D 四个备选项。
        3. 必须针对该知识点提取【考点标签】（字数严格限制在2-6个字之间）。
        4. 必须严格且仅返回以下JSON格式数组，绝不要包含任何额外的解释性文字：
        [
            {{
                "content": "题目描述",
                "option_a": "选项A内容",
                "option_b": "选项B内容",
                "option_c": "选项C内容",
                "option_d": "选项D内容",
                "correct_answer": "A",
                "knowledge_point": "考点词汇"
            }}
        ]
        """
        try:
            client = OpenAI(api_key="sk-3Olyx8Ft9DJOGVcAAbfpy8FT78UvncXOoJAdLOjRVlgrOj0h",
                            base_url="https://api.moonshot.cn/v1")
            response = client.chat.completions.create(
                model="moonshot-v1-8k",
                messages=[{"role": "system", "content": "你是智能组卷系统"}, {"role": "user", "content": prompt}],
                temperature=0.7
            )
            ai_text = response.choices[0].message.content.replace("```json", "").replace("```", "").strip()

            try:
                questions_data = json.loads(ai_text)
            except:
                start = ai_text.find('[')
                end = ai_text.rfind(']') + 1
                questions_data = json.loads(ai_text[start:end])

            generated_questions = []
            for q in questions_data:
                kp = q.get('knowledge_point', '').strip()
                if not kp or len(kp) > 8: kp = "AI定制考点"

                new_q = Question(
                    course_id=course.id, chapter_id=None,
                    content=q.get('content') or "内容缺失",
                    option_a=q.get('option_a') or "A", option_b=q.get('option_b') or "B",
                    option_c=q.get('option_c') or "C", option_d=q.get('option_d') or "D",
                    correct_answer=q.get('correct_answer') or "A",
                    knowledge_point=kp
                )
                db.session.add(new_q)
                db.session.flush()

                generated_questions.append({
                    'id': new_q.id,
                    'content': new_q.content,
                    'option_a': new_q.option_a, 'option_b': new_q.option_b,
                    'option_c': new_q.option_c, 'option_d': new_q.option_d
                })
            db.session.commit()

            return jsonify({'status': 'success', 'questions': generated_questions, 'course_id': course.id})

        except Exception as e:
            print(f"【AI报错详情】: {e}")
            return jsonify({'status': 'error', 'msg': f'AI 连接失败，请检查网络或更换 API Key。详情: {str(e)[:50]}'})

    @app.route('/submit_custom_quiz/<int:course_id>', methods=['POST'])
    def submit_custom_quiz(course_id):
        if 'user_id' not in session: return redirect(url_for('login'))
        user_id = session['user_id']
        course = db.session.get(Course, course_id)
        if not course: abort(404)

        score = 0
        result_details = []
        questions_count = 0

        for key, user_val in request.form.items():
            if key.startswith('q_'):
                q_id = int(key.split('_')[1])
                q = db.session.get(Question, q_id)
                if q:
                    questions_count += 1
                    is_correct = (user_val == q.correct_answer)
                    if is_correct:
                        score += 1
                    else:
                        existing_wrong = WrongQuestion.query.filter_by(user_id=user_id,
                                                                       question_content=q.content).first()
                        if not existing_wrong:
                            db.session.add(WrongQuestion(user_id=user_id, question_content=q.content))
                    result_details.append({'question': q, 'user_answer': user_val, 'is_correct': is_correct})

        db.session.add(LearningBehavior(user_id=user_id, course_id=course_id, action_type='quiz', duration=0))
        db.session.commit()

        class MockChapter:
            title = "🎯 AI 专属定制测验"

        return render_template('quiz_result.html', score=score, total=questions_count, result_details=result_details,
                               chapter=MockChapter(), course=course, is_redo=False)

    @app.route('/save_note', methods=['POST'])
    def save_note():
        if 'user_id' not in session: return jsonify({'status': 'error', 'msg': '未登录'})
        data = request.get_json()
        chapter_id = data.get('chapter_id')
        content = data.get('content')
        note = Note.query.filter_by(user_id=session['user_id'], chapter_id=chapter_id).first()
        if note:
            note.content = content
        else:
            new_note = Note(user_id=session['user_id'], chapter_id=chapter_id, content=content)
            db.session.add(new_note)
        db.session.commit()
        return jsonify({'status': 'success'})

    @app.route('/get_note/<int:chapter_id>')
    def get_note(chapter_id):
        if 'user_id' not in session: return jsonify({'status': 'error'})
        note = Note.query.filter_by(user_id=session['user_id'], chapter_id=chapter_id).first()
        return jsonify({'status': 'success', 'content': note.content if note else ''})

    @app.route('/record_behavior', methods=['POST'])
    def record_behavior():
        data = request.get_json()
        if 'user_id' in session:
            new_behavior = LearningBehavior(user_id=session['user_id'], course_id=data.get('course_id'),
                                            chapter_id=data.get('chapter_id'), action_type='watch_video',
                                            duration=data.get('duration'))
            db.session.add(new_behavior)
            db.session.commit()
            return jsonify({'status': 'success'})
        return jsonify({'status': 'error'}), 401

    @app.route('/quiz/chapter/<int:chapter_id>')
    def quiz_chapter(chapter_id):
        if 'user_id' not in session: return redirect(url_for('login'))
        chapter = db.session.get(Chapter, chapter_id)
        if not chapter: abort(404)
        questions = Question.query.filter_by(chapter_id=chapter_id).all()
        return render_template('quiz.html', course=chapter.course, chapter=chapter, questions=questions)

    @app.route('/submit_quiz/chapter/<int:chapter_id>', methods=['POST'])
    def submit_quiz_chapter(chapter_id):
        if 'user_id' not in session: return redirect(url_for('login'))
        user_id = session['user_id']
        chapter = db.session.get(Chapter, chapter_id)
        if not chapter: abort(404)
        questions = Question.query.filter_by(chapter_id=chapter_id).all()

        score = 0
        result_details = []

        for q in questions:
            user_val = request.form.get(f'q_{q.id}')
            is_correct = (user_val == q.correct_answer)
            if is_correct:
                score += 1
            else:
                existing_wrong = WrongQuestion.query.filter_by(user_id=user_id, question_content=q.content).first()
                if not existing_wrong:
                    wrong = WrongQuestion(user_id=user_id, question_content=q.content)
                    db.session.add(wrong)

            result_details.append({'question': q, 'user_answer': user_val, 'is_correct': is_correct})

        db.session.add(
            LearningBehavior(user_id=user_id, course_id=chapter.course_id, chapter_id=chapter.id, action_type='quiz',
                             duration=0))
        db.session.commit()

        return render_template('quiz_result.html', score=score, total=len(questions), result_details=result_details,
                               chapter=chapter, course=chapter.course, is_redo=False)

    @app.route('/api/analyze_question', methods=['POST'])
    def api_analyze_question():
        data = request.get_json()
        question_content = data.get('question')
        user_ans = data.get('user_answer')
        correct_ans = data.get('correct_answer')
        options = data.get('options')

        prompt = f"""
        请分析这道题：
        【题目】{question_content}
        【选项】{options}
        【学生选择】{user_ans}
        【正确答案】{correct_ans}

        请简要解释为什么正确答案是{correct_ans}，并指出学生可能的错误原因（如果选错）。100字以内。
        """
        try:
            client = OpenAI(api_key="sk-3Olyx8Ft9DJOGVcAAbfpy8FT78UvncXOoJAdLOjRVlgrOj0h",
                            base_url="https://api.moonshot.cn/v1")
            response = client.chat.completions.create(model="moonshot-v1-8k",
                                                      messages=[{"role": "system", "content": "智能助教"},
                                                                {"role": "user", "content": prompt}], temperature=0.7)
            return jsonify({'status': 'success', 'analysis': response.choices[0].message.content})
        except Exception as e:
            print(f"【AI报错详情】: {e}")
            return jsonify({'status': 'error', 'msg': f'AI 连接超时，详情: {str(e)[:50]}'})

    @app.route('/dashboard')
    def dashboard():
        if 'user_id' not in session: return redirect(url_for('login'))
        user_id = session['user_id']
        behaviors = LearningBehavior.query.filter_by(user_id=user_id).order_by(LearningBehavior.timestamp).all()

        raw_mistakes = WrongQuestion.query.filter_by(user_id=user_id).all()
        valid_mistakes_count = 0
        for m in raw_mistakes:
            if Question.query.filter_by(content=m.question_content).first():
                valid_mistakes_count += 1
            else:
                db.session.delete(m)
        db.session.commit()

        if not behaviors:
            return render_template('dashboard.html', dates=[], daily_durations=[], pie_data=[],
                                   suggestions=["还没有学习数据，请先去学习几门课程吧！"], courses=[])

        daily_dict = {}
        course_dict = {}
        total_duration = 0
        studied_course_ids = set()

        for b in behaviors:
            date_str = b.timestamp.strftime('%Y-%m-%d')
            daily_dict[date_str] = daily_dict.get(date_str, 0) + b.duration
            if b.course_id:
                course_dict[b.course_id] = course_dict.get(b.course_id, 0) + b.duration
                studied_course_ids.add(b.course_id)
            total_duration += b.duration

        dates = sorted(list(daily_dict.keys()))
        daily_durations = [daily_dict[d] // 60 for d in dates]

        pie_data = []
        filter_courses = []
        for cid, dur in course_dict.items():
            c = db.session.get(Course, cid)
            if c:
                pie_data.append({'name': c.title, 'value': dur // 60})
                filter_courses.append(c)

        suggestions = []
        total_duration_min = total_duration // 60
        suggestions.append(f"🤖 当前总计学习与活跃时长为 {total_duration_min} 分钟。")

        if valid_mistakes_count > 0:
            suggestions.append(f"⚠️ 错题本中有 {valid_mistakes_count} 道错题，建议进行【错题重做】巩固知识。")
        else:
            suggestions.append("🌟 恭喜！知识点掌握非常牢固。")

        return render_template('dashboard.html', dates=dates, daily_durations=daily_durations, pie_data=pie_data,
                               suggestions=suggestions, courses=filter_courses)

    @app.route('/ask_ai_tutor', methods=['POST'])
    def ask_ai_tutor():
        if 'user_id' not in session: return jsonify({'error': '未登录'}), 401
        user_id = session['user_id']

        data = request.get_json() or {}
        filter_course_id = data.get('course_id', 'all')

        if filter_course_id != 'all':
            cid = int(filter_course_id)
            course_title = db.session.get(Course, cid).title
            behaviors = LearningBehavior.query.filter_by(user_id=user_id, course_id=cid).all()
            mistakes = db.session.query(WrongQuestion).join(Question,
                                                            WrongQuestion.question_content == Question.content).filter(
                WrongQuestion.user_id == user_id, Question.course_id == cid).all()
            focus_str = f"针对《{course_title}》这门特定课程"
        else:
            behaviors = LearningBehavior.query.filter_by(user_id=user_id).all()
            mistakes = WrongQuestion.query.filter_by(user_id=user_id).all()
            focus_str = "针对全局各门课程"

        course_durations = {}
        for b in behaviors:
            if b.course_id:
                c = db.session.get(Course, b.course_id)
                if c:
                    course_durations[c.title] = course_durations.get(c.title, 0) + b.duration

        duration_info = ""
        for title, dur in course_durations.items():
            duration_info += f"- 《{title}》: 累计 {dur // 60} 分钟\n"
        if not duration_info:
            duration_info = "暂无学习时长记录"

        mistake_info = ""
        for m in mistakes:
            q = Question.query.filter_by(content=m.question_content).first()
            c_title = "未知课程"
            k_point = "未知考点"
            if q:
                if q.course_id:
                    course_obj = db.session.get(Course, q.course_id)
                    if course_obj: c_title = course_obj.title
                k_point = q.knowledge_point

            mistake_info += f"- [{c_title} | 考点:{k_point}] 题目：{m.question_content}\n"

        if not mistake_info:
            mistake_info = "太棒了，目前该区间下错题本为空！"

        prompt = f"""
        你是一名专业的AI私人辅导老师。请{focus_str}，根据以下该学生的最新真实数据，为他写一份深入的【靶向学习简报】。

        【时间投入分布】
        {duration_info}

        【待攻克错题及薄弱考点】
        {mistake_info}

        分析要求：
        1. 结合他的时间分配，评价学习侧重点或进度。
        2. 深度剖析错题内容（精准提炼其薄弱的核心知识点），帮他分析认知盲区。
        3. 给出极其具体的、可执行的下一步复习指导策略。
        4. 请直接使用清晰的纯文本加空行分段，严禁使用Markdown的星号（*）加粗，字数控制在300字左右，语气亲切具有鼓励性。
        """

        try:
            client = OpenAI(api_key="sk-3Olyx8Ft9DJOGVcAAbfpy8FT78UvncXOoJAdLOjRVlgrOj0h",
                            base_url="https://api.moonshot.cn/v1")
            response = client.chat.completions.create(
                model="moonshot-v1-8k",
                messages=[{"role": "system", "content": "你是智能导师"}, {"role": "user", "content": prompt}],
                temperature=0.7
            )
            formatted_advice = response.choices[0].message.content.replace('\n', '<br>')
            return jsonify({'status': 'success', 'advice': formatted_advice})
        except Exception as e:
            print(f"【AI报错详情】: {e}")
            return jsonify({'status': 'error', 'msg': f'AI 老师掉线，详情: {str(e)[:50]}'})

    @app.route('/my_mistakes')
    def my_mistakes():
        if 'user_id' not in session: return redirect(url_for('login'))
        user_id = session['user_id']
        mistakes = WrongQuestion.query.filter_by(user_id=user_id).order_by(WrongQuestion.timestamp.desc()).all()

        course_mistakes_map = {}
        total_count = 0

        for m in mistakes:
            q = Question.query.filter_by(content=m.question_content).first()
            if q:
                if q.course_id:
                    course_mistakes_map[q.course_id] = course_mistakes_map.get(q.course_id, 0) + 1
                total_count += 1
            else:
                db.session.delete(m)
        db.session.commit()

        course_data = []
        for cid, count in course_mistakes_map.items():
            c = db.session.get(Course, cid)
            if c:
                course_data.append({'course': c, 'mistake_count': count})

        return render_template('mistakes.html', course_data=course_data, total_count=total_count)

    @app.route('/view_mistakes/<int:course_id>')
    def view_mistakes(course_id):
        if 'user_id' not in session: return redirect(url_for('login'))
        user_id = session['user_id']
        course = db.session.get(Course, course_id)
        if not course: abort(404)

        mistakes = WrongQuestion.query.filter_by(user_id=user_id).order_by(WrongQuestion.timestamp.desc()).all()
        mistake_details = []

        for m in mistakes:
            q = Question.query.filter_by(content=m.question_content).first()
            if q and q.course_id == course_id:
                mistake_details.append({'mistake_id': m.id, 'question': q, 'timestamp': m.timestamp})

        return render_template('mistakes_detail.html', course=course, mistake_details=mistake_details)

    @app.route('/delete_mistake/<int:mistake_id>')
    def delete_mistake(mistake_id):
        if 'user_id' not in session: return redirect(url_for('login'))
        m = db.session.get(WrongQuestion, mistake_id)
        if m and m.user_id == session['user_id']:
            q = Question.query.filter_by(content=m.question_content).first()
            course_id = q.course_id if q else None
            db.session.delete(m)
            db.session.commit()
            if course_id:
                return redirect(url_for('view_mistakes', course_id=course_id))
        return redirect(url_for('my_mistakes'))

    @app.route('/redo_mistakes/<int:course_id>')
    def redo_mistakes(course_id):
        if 'user_id' not in session: return redirect(url_for('login'))
        user_id = session['user_id']
        course = db.session.get(Course, course_id)
        if not course: abort(404)

        mistakes = WrongQuestion.query.filter_by(user_id=user_id).all()
        questions = []

        for m in mistakes:
            q = Question.query.filter_by(content=m.question_content).first()
            if q and q.course_id == course_id:
                questions.append(q)

        if not questions:
            flash(f'《{course.title}》 的错题本是空的，无需重做！')
            return redirect(url_for('my_mistakes'))

        return render_template('redo_mistakes.html', course=course, questions=questions)

    @app.route('/submit_redo_mistakes/<int:course_id>', methods=['POST'])
    def submit_redo_mistakes(course_id):
        if 'user_id' not in session: return redirect(url_for('login'))
        user_id = session['user_id']
        course = db.session.get(Course, course_id)
        if not course: abort(404)

        mistakes = WrongQuestion.query.filter_by(user_id=user_id).all()
        score = 0;
        total = 0;
        cleared_count = 0
        result_details = []

        for m in mistakes:
            q = Question.query.filter_by(content=m.question_content).first()
            if not q or q.course_id != course_id: continue

            total += 1
            user_val = request.form.get(f'q_{q.id}')
            is_correct = (user_val == q.correct_answer)
            if is_correct:
                score += 1
                db.session.delete(m)
                cleared_count += 1
            result_details.append({'question': q, 'user_answer': user_val, 'is_correct': is_correct})

        db.session.commit()
        if cleared_count > 0:
            flash(f'🎉 太棒了！你本次成功攻克了《{course.title}》的 {cleared_count} 道错题！')

        return render_template('quiz_result.html', score=score, total=total, result_details=result_details,
                               course=course, is_redo=True)

    with app.app_context():
        db.create_all()

        try:
            db.session.execute(db.text('ALTER TABLE user ADD COLUMN weekly_goal INTEGER DEFAULT 120;'))
            db.session.commit()
            print("🚀 [系统提示]：数据库已自动升级，添加自主学习目标字段！")
        except Exception:
            db.session.rollback()

        if not User.query.filter_by(username='admin').first():
            db.session.add(User(username='admin', password='admin', role='teacher'))
        if not Course.query.first():
            c = Course(title="Python 数据分析基础")
            db.session.add(c)
            db.session.commit()
            ch1 = Chapter(course_id=c.id, title="1.1 搭建与安装", video_url="videos/demo.mp4")
            db.session.add(ch1)
            db.session.commit()

    return app


def open_browser(): webbrowser.open_new('http://127.0.0.1:5000')


if __name__ == '__main__':
    app = create_app()
    if not os.environ.get("WERKZEUG_RUN_MAIN"): Timer(1.5, open_browser).start()
    app.run(debug=True, port=5000)