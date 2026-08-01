#!/bin/bash
# Oracle: arrange the initial workspace files into the exact GT structure the
# grader (evaluation/check_local.py GT_STRUCTURE) checks. There is no
# groundtruth_workspace upstream — the grader hardcodes the target tree — so
# the oracle scripts the moves directly.
set -e
cd /app

# Target directory tree (including dirs that end up empty).
mkdir -p \
  Entertainment/Movies \
  Entertainment/Music \
  "Entertainment/Pictures/Year-2025/Landscape" \
  "Entertainment/Pictures/Year-2025/People" \
  "Entertainment/Pictures/Year-2025/Pets" \
  School/Applications_Materials \
  School/Courses_Materials \
  School/Graduation_Projects \
  School/Language_Exam_Preparation \
  Work/Job_Application_Materials \
  Work/Offer_Galary \
  Work/Software \
  Work/Projects

# Entertainment
mv Work/Movie_The_Wandering_Earth.mp4        Entertainment/Movies/
mv Entertainment/TV_Show_Friends_S01E01.mkv  Entertainment/Movies/
mv Entertainment/Music_Jay_Chou_Best.mp3     Entertainment/Music/
mv mount.png                                 "Entertainment/Pictures/Year-2025/Landscape/"
mv sichuan_lake.png                          "Entertainment/Pictures/Year-2025/Landscape/"
mv cat.png                                   "Entertainment/Pictures/Year-2025/Pets/"

# School
mv Recommendation_Letter_1.pdf               School/Applications_Materials/
mv Recommendation_Letter_2.pdf               School/Applications_Materials/
mv cv-gboeing.pdf                            School/Applications_Materials/
mv exam.xlsx                                 School/Courses_Materials/
mv course_model_weight_1.png                 School/Courses_Materials/
mv course_model_weight_2.png                 School/Courses_Materials/
mv course_model_weight_3.png                 School/Courses_Materials/
mv Work/Calculus_Final_Review.ppt            School/Courses_Materials/
mv Course_Schedule.jpg                       School/Courses_Materials/
mv course_schedule.xls                       School/Courses_Materials/
mv Entertainment/Machine_Learning_Course_Notes.md School/Courses_Materials/
mv Graduation_Materials_Notice_202506.doc    School/Graduation_Projects/
mv Listening1-3.mp3                          School/Language_Exam_Preparation/

# Work
mv Internship_application_form.xlsx          Work/Job_Application_Materials/
mv Clash.Verge_2.0.3-alpha_aarch64.dmg       Work/Software/
mv Entertainment/Product_Design_Proposal.pptx Work/Projects/

echo "workspace arranged"
