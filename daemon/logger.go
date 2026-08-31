package main

import (
	golog "log"
)

type aimlessLogger struct {
	verbose bool
}

func (l *aimlessLogger) Printf(format string, args ...interface{}) {
	golog.Printf(format, args...)
}

func (l *aimlessLogger) Println(args ...interface{}) {
	golog.Println(args...)
}

func (l *aimlessLogger) Infof(format string, args ...interface{}) {
	golog.Printf("INFO "+format, args...)
}

func (l *aimlessLogger) Infoln(args ...interface{}) {
	golog.Println(append([]interface{}{"INFO"}, args...)...)
}

func (l *aimlessLogger) Warnf(format string, args ...interface{}) {
	golog.Printf("WARN "+format, args...)
}

func (l *aimlessLogger) Warnln(args ...interface{}) {
	golog.Println(append([]interface{}{"WARN"}, args...)...)
}

func (l *aimlessLogger) Errorf(format string, args ...interface{}) {
	golog.Printf("ERROR "+format, args...)
}

func (l *aimlessLogger) Errorln(args ...interface{}) {
	golog.Println(append([]interface{}{"ERROR"}, args...)...)
}

func (l *aimlessLogger) Debugf(format string, args ...interface{}) {
	if l.verbose {
		golog.Printf("DEBUG "+format, args...)
	}
}

func (l *aimlessLogger) Debugln(args ...interface{}) {
	if l.verbose {
		golog.Println(append([]interface{}{"DEBUG"}, args...)...)
	}
}

func (l *aimlessLogger) Traceln(args ...interface{}) {
	if l.verbose {
		golog.Println(append([]interface{}{"TRACE"}, args...)...)
	}
}
