// Bounded local experiment driver, executed in the ordinary GUI agent.
// Never uses PID-directed input, Apple Events or writes the editor's text.
- (void)disconnectCapture:(NSString *)reason {
    if(self.scriptTimer) { [self record:@"script_aborted" values:@{@"reason":@"disconnected"}];[self.scriptTimer invalidate];self.scriptTimer=nil; }
    if(self.originalInputSource) {selectInputSource(self.originalInputSource);self.originalInputSource=nil;}
    self.connected=NO;self.epoch++;[self.inputTimer invalidate];self.inputTimer=nil;
    SCStream *old=self.stream;self.stream=nil;
    if(self.capturing)[old stopCaptureWithCompletionHandler:^(NSError *error){(void)error;}];self.capturing=NO;
    [self record:@"disconnected" values:@{@"epoch":@(self.epoch),@"reason":reason}];
}
- (NSDictionary *)editorState {
    NSString *caseID=self.scriptRequest[@"case_id"];
    if(!validRun(caseID))return nil;
    NSString *path=[NSHomeDirectory() stringByAppendingPathComponent:[NSString stringWithFormat:@"Library/Application Support/ZMacRDS/Editor/%@/state.json",caseID]];
    struct stat s;if(lstat(path.fileSystemRepresentation,&s)!=0 || !S_ISREG(s.st_mode) || s.st_uid!=getuid() || s.st_size>65536)return nil;
    NSDictionary *state=[NSJSONSerialization JSONObjectWithData:[NSData dataWithContentsOfFile:path] options:0 error:NULL];
    if(![state isKindOfClass:NSDictionary.class])return nil;
    if(![state[@"uid"] isEqual:@(getuid())] || ![state[@"audit_session_id"] isEqual:self.initialIdentity[@"audit_session_id"]] ||
       ![state[@"boot_id"] isEqual:kernelString("kern.bootsessionuuid")] ||
       nowMS()-[state[@"uptime_ms"] unsignedLongLongValue]>3500)return nil;
    NSRunningApplication *app=[NSRunningApplication runningApplicationWithProcessIdentifier:[state[@"pid"] intValue]];
    if(![app.bundleIdentifier isEqual:@"org.zmacrds.m1-editor"])return nil;
    return state;
}
- (void)beginScript {
    NSString *path=[self.directory stringByAppendingPathComponent:@"request-script.json"];
    struct stat s;
    if(lstat(path.fileSystemRepresentation,&s)!=0)return;
    if(!S_ISREG(s.st_mode) || s.st_uid!=getuid() || s.st_size>131072){[self record:@"script_rejected" values:@{@"reason":@"invalid file"}];return;}
    NSData *data=[NSData dataWithContentsOfFile:path];unlink(path.fileSystemRepresentation);
    NSDictionary *request=[NSJSONSerialization JSONObjectWithData:data options:0 error:NULL];
    NSString *reason=nil;
    if(![request isKindOfClass:NSDictionary.class]) reason=@"invalid JSON";
    else if(![request[@"nonce"] isEqual:@(self.nonce)] || ![request[@"epoch"] isEqual:@(self.epoch)] ||
            ![request[@"audit_session_id"] isEqual:self.initialIdentity[@"audit_session_id"]] ||
            ![request[@"boot_id"] isEqual:kernelString("kern.bootsessionuuid")] || ![request[@"uid"] isEqual:@(getuid())]) reason=@"stale identity or epoch";
    else if(!self.connected || !self.capturing || !AXIsProcessTrusted() || self.scriptTimer || self.inputTimer) reason=@"not ready";
    NSArray *actions=[request isKindOfClass:NSDictionary.class] ? request[@"actions"] : nil;
    if(!reason && (![actions isKindOfClass:NSArray.class] || actions.count==0 || actions.count>2000))reason=@"invalid actions";
    NSSet *types=[NSSet setWithArray:@[@"focus",@"text",@"key",@"source",@"delay",@"checkpoint",@"drag"]];
    if(!reason)for(id action in actions) {
        if(![action isKindOfClass:NSDictionary.class] || ![types containsObject:action[@"type"]]){reason=@"unknown action";break;}
        NSString *type=action[@"type"];
        if([type isEqual:@"text"] && (![action[@"text"] isKindOfClass:NSString.class] || [action[@"text"] length]<1 || [action[@"text"] length]>20))reason=@"invalid text";
        if([type isEqual:@"key"] && (![action[@"code"] isKindOfClass:NSNumber.class] || [action[@"code"] intValue]<0 || [action[@"code"] intValue]>127))reason=@"invalid key";
        if([type isEqual:@"focus"] && ![@[@"agent",@"editor"] containsObject:action[@"target"]])reason=@"invalid focus";
        if([type isEqual:@"source"] && ![@[@"com.apple.keylayout.ABC",@"com.apple.inputmethod.SCIM.ITABC"] containsObject:action[@"id"]])reason=@"invalid input source";
        if([type isEqual:@"delay"] && (![action[@"ms"] isKindOfClass:NSNumber.class] || [action[@"ms"] intValue]<0 || [action[@"ms"] intValue]>10000))reason=@"invalid delay";
    }
    if(reason){[self record:@"script_rejected" values:@{@"reason":reason}];return;}
    self.scriptRequest=request;
    NSDictionary *editor=[self editorState];
    if(!editor){[self record:@"script_rejected" values:@{@"reason":@"editor identity not current"}];self.scriptRequest=nil;return;}
    self.script=actions;self.scriptIndex=0;self.originalInputSource=inputSourceID();self.nextActionAt=0;
    [self record:@"script_started" values:@{@"case_id":request[@"case_id"],@"editor_pid":editor[@"pid"],@"editor_nonce":editor[@"nonce"],@"epoch":@(self.epoch),@"actions":@(actions.count)}];
    self.scriptTimer=[NSTimer scheduledTimerWithTimeInterval:0.2 target:self selector:@selector(scriptTick:) userInfo:nil repeats:YES];
}
- (void)postScriptEvent:(CGEventRef)event {
    uint64_t tag=((uint64_t)self.nonce<<32)|(++self.scriptEvent);
    CGEventSetIntegerValueField(event,kCGEventSourceUserData,(int64_t)tag);
    [self record:@"script_post" values:@{@"tag":[NSString stringWithFormat:@"%016llx",(unsigned long long)tag],@"event_type":@(CGEventGetType(event)),@"action":@(self.scriptIndex-1),@"epoch":@(self.epoch),@"route":@"kCGSessionEventTap"}];
    CGEventPost(kCGSessionEventTap,event);CFRelease(event);
}
- (void)scriptTick:(NSTimer *)timer {
    if(nowMS()<self.nextActionAt)return;
    NSDictionary *editor=[self editorState],*who=identity();
    pid_t front=NSWorkspace.sharedWorkspace.frontmostApplication.processIdentifier;
    NSString *reason=nil;
    if(!self.connected || !AXIsProcessTrusted() || !CGPreflightScreenCaptureAccess())reason=@"permission or connection lost";
    else if(![who[@"audit_session_id"] isEqual:self.initialIdentity[@"audit_session_id"]] || !editor)reason=@"session or editor identity changed";
    else if(front!=getpid() && front!=[editor[@"pid"] intValue])reason=@"unexpected focused application";
    if(reason){[self record:@"script_aborted" values:@{@"reason":reason}];[timer invalidate];self.scriptTimer=nil;if(self.originalInputSource)selectInputSource(self.originalInputSource);self.originalInputSource=nil;return;}
    if(self.scriptIndex>=self.script.count){[self record:@"script_finished" values:@{@"editor_text":editor[@"text"],@"editor_pid":editor[@"pid"],@"epoch":@(self.epoch)}];[timer invalidate];self.scriptTimer=nil;if(self.originalInputSource)selectInputSource(self.originalInputSource);self.originalInputSource=nil;return;}
    NSDictionary *action=self.script[self.scriptIndex++];NSString *type=action[@"type"];
    [self record:@"script_action" values:@{@"index":@(self.scriptIndex-1),@"action":action,@"frontmost_pid":@(front),@"epoch":@(self.epoch)}];
    if([type isEqual:@"delay"]){self.nextActionAt=nowMS()+[action[@"ms"] unsignedLongLongValue];return;}
    if([type isEqual:@"checkpoint"]){
        BOOL ok=(!action[@"text"] || [action[@"text"] isEqual:editor[@"text"]]) && (!action[@"focus"] || ([action[@"focus"] isEqual:@"editor"] ? front==[editor[@"pid"] intValue] : front==getpid())) && (!action[@"selection_min"] || [editor[@"selection_length"] unsignedIntegerValue]>=[action[@"selection_min"] unsignedIntegerValue]);
        [self record:@"checkpoint" values:@{@"label":action[@"label"] ?: @"",@"ok":@(ok),@"editor":editor,@"frontmost_pid":@(front)}];
        if(!ok){[self record:@"script_aborted" values:@{@"reason":@"checkpoint mismatch"}];[timer invalidate];self.scriptTimer=nil;if(self.originalInputSource)selectInputSource(self.originalInputSource);self.originalInputSource=nil;}
        return;
    }
    if([type isEqual:@"source"]){BOOL ok=selectInputSource(action[@"id"]);[self record:@"input_source_selected" values:@{@"ok":@(ok),@"id":inputSourceID()}];if(!ok){[self record:@"script_aborted" values:@{@"reason":@"input source unavailable"}];[timer invalidate];self.scriptTimer=nil;}return;}
    CGEventSourceRef source=CGEventSourceCreate(kCGEventSourceStatePrivate);
    if([type isEqual:@"focus"] || [type isEqual:@"drag"]){
        // Both probe windows have fixed, recorded single-display geometry.
        CGFloat height=NSMaxY(NSScreen.screens.firstObject.frame);
        CGPoint start=CGPointMake(1000,height-650),end=CGPointMake(1350,height-650);
        if([type isEqual:@"drag"]){start=CGPointMake(920,height-782);end=CGPointMake(1320,height-782);}
        if([action[@"target"] isEqual:@"agent"]){NSPoint p=[self.window convertPointToScreen:NSMakePoint(100,100)];start=CGPointMake(p.x,height-p.y);}
        [self postScriptEvent:CGEventCreateMouseEvent(source,kCGEventLeftMouseDown,start,kCGMouseButtonLeft)];
        if([type isEqual:@"drag"])[self postScriptEvent:CGEventCreateMouseEvent(source,kCGEventLeftMouseDragged,end,kCGMouseButtonLeft)];else end=start;
        [self postScriptEvent:CGEventCreateMouseEvent(source,kCGEventLeftMouseUp,end,kCGMouseButtonLeft)];
    } else {
        if(front!=[editor[@"pid"] intValue]){[self record:@"script_aborted" values:@{@"reason":@"editor not focused"}];[timer invalidate];self.scriptTimer=nil;CFRelease(source);return;}
        CGKeyCode code=[action[@"code"] unsignedShortValue];
        CGEventFlags flags=0;if([action[@"command"] boolValue])flags|=kCGEventFlagMaskCommand;if([action[@"shift"] boolValue])flags|=kCGEventFlagMaskShift;
        for(int down=1;down>=0;down--){CGEventRef event=CGEventCreateKeyboardEvent(source,code,down);CGEventSetFlags(event,flags);
            if([type isEqual:@"text"]){NSString *text=action[@"text"];UniChar chars[20];[text getCharacters:chars range:NSMakeRange(0,text.length)];CGEventKeyboardSetUnicodeString(event,text.length,chars);}
            [self postScriptEvent:event];}
    }
    if(source)CFRelease(source);
}
