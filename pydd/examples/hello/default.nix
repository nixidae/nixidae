{ pydd, bash }:
pydd.mkPlanner {
  name = "hello";
  plan = ./plan.py;
  menu.bash = "${bash}";
}
