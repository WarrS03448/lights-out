// Graph builder: turns a compact JSON description into Blueprint nodes and wires, using the editor's own node classes.
// Driven from Scripts/make_blueprints.py through UBodycamMirrorTools::BuildGraph. Editor-only; nothing here ships.
#include "BodycamMirrorTools.h"
#include "Engine/Blueprint.h"
#include "EdGraph/EdGraph.h"
#include "EdGraph/EdGraphNode.h"
#include "EdGraph/EdGraphPin.h"
#include "EdGraphSchema_K2.h"
#include "Kismet2/BlueprintEditorUtils.h"
#include "Kismet2/KismetEditorUtilities.h"
#include "K2Node_CallFunction.h"
#include "K2Node_CallArrayFunction.h"
#include "K2Node_CallParentFunction.h"
#include "K2Node_Event.h"
#include "K2Node_CustomEvent.h"
#include "K2Node_VariableGet.h"
#include "K2Node_VariableSet.h"
#include "K2Node_DynamicCast.h"
#include "K2Node_IfThenElse.h"
#include "K2Node_ExecutionSequence.h"
#include "K2Node_MacroInstance.h"
#include "K2Node_ConstructObjectFromClass.h"
#include "K2Node_SpawnActorFromClass.h"
#include "BlueprintNodeSpawner.h"
#include "K2Node_BreakStruct.h"
#include "K2Node_MakeStruct.h"
#include "K2Node_MakeArray.h"
#include "K2Node_MakeMap.h"
#include "K2Node_Self.h"
#include "K2Node_AddDelegate.h"
#include "K2Node_RemoveDelegate.h"
#include "K2Node_AsyncAction.h"
#include "K2Node_CreateDelegate.h"
#include "K2Node_FunctionEntry.h"
#include "K2Node_FunctionResult.h"
#include "K2Node_EditablePinBase.h"
#include "Dom/JsonObject.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "UObject/UnrealType.h"
#include "UObject/UObjectGlobals.h"

namespace {

struct FBuildCtx {
    UBlueprint* BP = nullptr;
    UEdGraph* Graph = nullptr;
    const UEdGraphSchema_K2* Schema = nullptr;
    TMap<FString, UEdGraphNode*> Nodes;
    TArray<FString> Log;
    int32 Errors = 0;
    // "createevent" nodes wait here until every link is made: UK2Node_CreateDelegate validates the
    // function it is given against the DELEGATE PIN it feeds, and that pin has no signature until
    // something is connected to it. Binding at creation time silently clears the selection.
    struct FPendingDelegate { class UK2Node_CreateDelegate* Node; FName Func; FString Id; };
    TArray<FPendingDelegate> PendingDelegates;
    void Err(const FString& S) { Errors++; Log.Add(TEXT("ERROR: ") + S); }
    void Info(const FString& S) { Log.Add(S); }
};

UObject* LoadPath(const FString& Path) {
    if (Path.IsEmpty()) return nullptr;
    UObject* Obj = FindObject<UObject>(nullptr, *Path);
    if (!Obj) Obj = LoadObject<UObject>(nullptr, *Path);
    return Obj;
}
UClass* LoadClassPath(const FString& Path) { return Cast<UClass>(LoadPath(Path)); }
UScriptStruct* LoadStructPath(const FString& Path) { return Cast<UScriptStruct>(LoadPath(Path)); }

FEdGraphPinType PinTypeFromJson(const TSharedPtr<FJsonObject>& J) {
    FEdGraphPinType T;
    const FString Cat = J->GetStringField(TEXT("category")).ToLower();
    if (Cat == TEXT("bool")) T.PinCategory = UEdGraphSchema_K2::PC_Boolean;
    else if (Cat == TEXT("byte")) T.PinCategory = UEdGraphSchema_K2::PC_Byte;
    else if (Cat == TEXT("int")) T.PinCategory = UEdGraphSchema_K2::PC_Int;
    else if (Cat == TEXT("float") || Cat == TEXT("real") || Cat == TEXT("double")) { T.PinCategory = UEdGraphSchema_K2::PC_Real; T.PinSubCategory = UEdGraphSchema_K2::PC_Double; }
    else if (Cat == TEXT("float32")) { T.PinCategory = UEdGraphSchema_K2::PC_Real; T.PinSubCategory = UEdGraphSchema_K2::PC_Float; }
    else if (Cat == TEXT("name")) T.PinCategory = UEdGraphSchema_K2::PC_Name;
    else if (Cat == TEXT("string")) T.PinCategory = UEdGraphSchema_K2::PC_String;
    else if (Cat == TEXT("object")) { T.PinCategory = UEdGraphSchema_K2::PC_Object; T.PinSubCategoryObject = LoadClassPath(J->GetStringField(TEXT("class"))); }
    else if (Cat == TEXT("class")) { T.PinCategory = UEdGraphSchema_K2::PC_Class; T.PinSubCategoryObject = LoadClassPath(J->GetStringField(TEXT("class"))); }
    else if (Cat == TEXT("struct")) { T.PinCategory = UEdGraphSchema_K2::PC_Struct; T.PinSubCategoryObject = LoadStructPath(J->GetStringField(TEXT("struct"))); }
    if (J->HasField(TEXT("array")) && J->GetBoolField(TEXT("array"))) T.ContainerType = EPinContainerType::Array;
    // MAP pins. A map pin is not just a container flag: the KEY type lives in PinCategory (built
    // above, exactly as for a plain pin) and the VALUE type lives in a separate PinValueType, which
    // is a different struct with its own fields. Without this the builder can only express arrays,
    // which is why the game's TMap<FString, FBodycamLobbyAttribute> returns were unreachable -
    // MakeCreateLobbyParams' output and a search result's LobbySettings both need it.
    else if (J->HasField(TEXT("map"))) {
        const TSharedPtr<FJsonObject>* V = nullptr;
        if (J->TryGetObjectField(TEXT("map"), V) && V && V->IsValid()) {
            T.ContainerType = EPinContainerType::Map;
            const FEdGraphPinType VT = PinTypeFromJson(*V);
            T.PinValueType.TerminalCategory = VT.PinCategory;
            T.PinValueType.TerminalSubCategory = VT.PinSubCategory;
            T.PinValueType.TerminalSubCategoryObject = VT.PinSubCategoryObject;
        }
    }
    return T;
}

template <typename T> T* NewNode(FBuildCtx& C) {
    T* Node = NewObject<T>(C.Graph);
    C.Graph->AddNode(Node, false, false);
    Node->CreateNewGuid();
    return Node;
}
template <typename T> T* SpawnLikeEditor(FBuildCtx& C, int32 Index) {
    UBlueprintNodeSpawner* Spawner = UBlueprintNodeSpawner::Create(T::StaticClass());
    IBlueprintNodeBinder::FBindingSet Bindings;
    UEdGraphNode* Node = Spawner ? Spawner->Invoke(C.Graph, Bindings, FVector2D(420 * (Index % 7), 260 * (Index / 7))) : nullptr;
    return Cast<T>(Node);
}
void FinishNode(UEdGraphNode* Node, int32 Index) {
    Node->PostPlacedNewNode();
    Node->AllocateDefaultPins();
    Node->NodePosX = 420 * (Index % 7);
    Node->NodePosY = 260 * (Index / 7);
}

UEdGraphPin* FindPinLoose(UEdGraphNode* Node, const FString& Name, EEdGraphPinDirection Dir = EGPD_MAX) {
    // exact, then case-insensitive, then friendly name, then a few aliases
    for (UEdGraphPin* P : Node->Pins) if (P && P->PinName.ToString() == Name && (Dir == EGPD_MAX || P->Direction == Dir)) return P;
    for (UEdGraphPin* P : Node->Pins) if (P && P->PinName.ToString().Equals(Name, ESearchCase::IgnoreCase) && (Dir == EGPD_MAX || P->Direction == Dir)) return P;
    for (UEdGraphPin* P : Node->Pins) if (P && P->PinFriendlyName.ToString().Equals(Name, ESearchCase::IgnoreCase) && (Dir == EGPD_MAX || P->Direction == Dir)) return P;
    const FString L = Name.ToLower();
    if (L == TEXT("exec")) return Node->FindPin(UEdGraphSchema_K2::PN_Execute, EGPD_Input);
    if (L == TEXT("then")) return Node->FindPin(UEdGraphSchema_K2::PN_Then, EGPD_Output);
    if (L == TEXT("else")) return Node->FindPin(UEdGraphSchema_K2::PN_Else, EGPD_Output);
    if (L == TEXT("condition")) return Node->FindPin(UEdGraphSchema_K2::PN_Condition, EGPD_Input);
    if (L == TEXT("self") || L == TEXT("target")) return Node->FindPin(UEdGraphSchema_K2::PN_Self, EGPD_Input);
    if (L == TEXT("return") || L == TEXT("returnvalue")) return Node->FindPin(UEdGraphSchema_K2::PN_ReturnValue, EGPD_Output);
    if (UK2Node_DynamicCast* CastNode = Cast<UK2Node_DynamicCast>(Node)) {
        if (L == TEXT("cast_object") || L == TEXT("object")) return CastNode->GetCastSourcePin();
        if (L == TEXT("cast_result") || L == TEXT("as")) return CastNode->GetCastResultPin();
        if (L == TEXT("cast_ok")) return CastNode->IsNodePure() ? CastNode->GetBoolSuccessPin() : CastNode->GetValidCastPin();   // pure cast: the bSuccess data pin
        if (L == TEXT("cast_fail")) return CastNode->GetInvalidCastPin();
    }
    if (L == TEXT("in")) { for (UEdGraphPin* P : Node->Pins) if (P && P->Direction == EGPD_Input && P->PinType.PinCategory != UEdGraphSchema_K2::PC_Exec) return P; }
    if (L == TEXT("out")) { for (UEdGraphPin* P : Node->Pins) if (P && P->Direction == EGPD_Output && P->PinType.PinCategory != UEdGraphSchema_K2::PC_Exec) return P; }
    return nullptr;
}

FString PinList(UEdGraphNode* Node) {
    FString S;
    for (UEdGraphPin* P : Node->Pins) if (P) S += FString::Printf(TEXT("%s%s(%s) "), P->Direction == EGPD_Input ? TEXT(">") : TEXT("<"), *P->PinName.ToString(), *P->PinType.PinCategory.ToString());
    return S;
}

UClass* ResolveOwnerClass(FBuildCtx& C, const TSharedPtr<FJsonObject>& J) {
    FString ClassPath;
    if (J->TryGetStringField(TEXT("class"), ClassPath) && !ClassPath.IsEmpty()) {
        UClass* Cls = LoadClassPath(ClassPath);
        if (!Cls) C.Err(FString::Printf(TEXT("class not found: %s"), *ClassPath));
        return Cls;
    }
    return nullptr;   // = self
}

UEdGraphNode* MakeNode(FBuildCtx& C, const TSharedPtr<FJsonObject>& J, int32 Index) {
    const FString Id = J->GetStringField(TEXT("id"));
    const FString Type = J->GetStringField(TEXT("type")).ToLower();
    UClass* SelfClass = C.BP->SkeletonGeneratedClass ? C.BP->SkeletonGeneratedClass : C.BP->ParentClass;

    if (Type == TEXT("event")) {
        UClass* Owner = ResolveOwnerClass(C, J); if (!Owner) Owner = C.BP->ParentClass;
        const FName Name(*J->GetStringField(TEXT("name")));
        UFunction* Func = Owner->FindFunctionByName(Name);
        if (!Func) { C.Err(FString::Printf(TEXT("%s: event function %s not found on %s"), *Id, *Name.ToString(), *Owner->GetName())); return nullptr; }
        // a new Blueprint may already carry a disabled "ghost" node for this event (BeginPlay, Tick, ActorBeginOverlap): reuse it
        for (UEdGraphNode* Existing : C.Graph->Nodes) {
            UK2Node_Event* EV = Cast<UK2Node_Event>(Existing);
            if (!EV || Cast<UK2Node_CustomEvent>(Existing) || !EV->bOverrideFunction || EV->EventReference.GetMemberName() != Name) continue;
            EV->SetEnabledState(ENodeEnabledState::Enabled, false);
            EV->NodeComment.Empty(); EV->bCommentBubbleVisible = false;
            C.Info(FString::Printf(TEXT("%s: reusing the existing %s event node"), *Id, *Name.ToString()));
            return EV;
        }
        UK2Node_Event* N = NewNode<UK2Node_Event>(C);
        N->EventReference.SetExternalMember(Name, Func->GetOwnerClass());
        N->bOverrideFunction = true;
        FinishNode(N, Index); return N;
    }
    if (Type == TEXT("customevent")) {
        UK2Node_CustomEvent* N = NewNode<UK2Node_CustomEvent>(C);
        N->CustomFunctionName = FName(*J->GetStringField(TEXT("name")));
        FinishNode(N, Index);
        const TArray<TSharedPtr<FJsonValue>>* Params;
        if (J->TryGetArrayField(TEXT("params"), Params)) {
            for (const TSharedPtr<FJsonValue>& PV : *Params) {
                const TSharedPtr<FJsonObject>& PJ = PV->AsObject();
                N->CreateUserDefinedPin(FName(*PJ->GetStringField(TEXT("name"))), PinTypeFromJson(PJ), EGPD_Output);
            }
        }
        return N;
    }
    if (Type == TEXT("async")) {
        UClass* Owner = ResolveOwnerClass(C, J);
        UFunction* Func = Owner ? Owner->FindFunctionByName(FName(*J->GetStringField(TEXT("func")))) : nullptr;
        if (!Func) { C.Err(Id + TEXT(": async factory function not found")); return nullptr; }
        UK2Node_AsyncAction* N = NewNode<UK2Node_AsyncAction>(C);
        N->InitializeProxyFromFunction(Func);
        FinishNode(N, Index); return N;
    }
    if (Type == TEXT("call") || Type == TEXT("callparent")) {
        UClass* Owner = ResolveOwnerClass(C, J); if (!Owner) Owner = SelfClass;
        const FName FuncName(*J->GetStringField(TEXT("func")));
        UFunction* Func = Owner ? Owner->FindFunctionByName(FuncName) : nullptr;
        const TArray<TSharedPtr<FJsonValue>>* Alts = nullptr;
        if (!Func && Owner && J->TryGetArrayField(TEXT("alt"), Alts)) {
            for (const TSharedPtr<FJsonValue>& A : *Alts) { Func = Owner->FindFunctionByName(FName(*A->AsString())); if (Func) { C.Info(FString::Printf(TEXT("%s: using %s"), *Id, *A->AsString())); break; } }
        }
        if (!Func) { C.Err(FString::Printf(TEXT("%s: function %s not found on %s"), *Id, *FuncName.ToString(), Owner ? *Owner->GetName() : TEXT("?"))); return nullptr; }
        UK2Node_CallFunction* N;
        if (Type == TEXT("callparent")) N = NewNode<UK2Node_CallParentFunction>(C);
        else if (J->HasField(TEXT("array")) && J->GetBoolField(TEXT("array"))) N = NewNode<UK2Node_CallArrayFunction>(C);
        else N = NewNode<UK2Node_CallFunction>(C);
        N->SetFromFunction(Func);
        FinishNode(N, Index); return N;
    }
    if (Type == TEXT("get") || Type == TEXT("set")) {
        UClass* Owner = ResolveOwnerClass(C, J);
        const FName VarName(*J->GetStringField(TEXT("var")));
        UK2Node_Variable* N = Type == TEXT("get") ? (UK2Node_Variable*)NewNode<UK2Node_VariableGet>(C) : (UK2Node_Variable*)NewNode<UK2Node_VariableSet>(C);
        if (Owner) N->VariableReference.SetExternalMember(VarName, Owner);
        else N->VariableReference.SetSelfMember(VarName);
        FinishNode(N, Index);
        if (N->Pins.Num() == 0) C.Err(FString::Printf(TEXT("%s: variable %s produced no pins (unknown variable?)"), *Id, *VarName.ToString()));
        return N;
    }
    if (Type == TEXT("cast")) {
        UClass* Target = LoadClassPath(J->GetStringField(TEXT("class")));
        if (!Target) { C.Err(FString::Printf(TEXT("%s: cast class not found"), *Id)); return nullptr; }
        UK2Node_DynamicCast* N = NewNode<UK2Node_DynamicCast>(C);
        N->TargetType = Target;
        if (J->HasField(TEXT("pure")) && J->GetBoolField(TEXT("pure"))) N->SetPurity(true);
        FinishNode(N, Index); return N;
    }
    if (Type == TEXT("branch")) { UK2Node_IfThenElse* N = NewNode<UK2Node_IfThenElse>(C); FinishNode(N, Index); return N; }
    if (Type == TEXT("sequence")) {
        UK2Node_ExecutionSequence* N = NewNode<UK2Node_ExecutionSequence>(C); FinishNode(N, Index);
        const int32 Count = J->HasField(TEXT("count")) ? (int32)J->GetNumberField(TEXT("count")) : 2;
        for (int32 i = 2; i < Count; ++i) N->AddInputPin();
        return N;
    }
    if (Type == TEXT("foreach") || Type == TEXT("macro")) {
        const FString MacroName = Type == TEXT("foreach") ? TEXT("ForEachLoop") : J->GetStringField(TEXT("name"));
        UBlueprint* Macros = LoadObject<UBlueprint>(nullptr, TEXT("/Engine/EditorBlueprintResources/StandardMacros.StandardMacros"));
        UEdGraph* MacroGraph = nullptr;
        if (Macros) for (UEdGraph* G : Macros->MacroGraphs) if (G && G->GetName() == MacroName) MacroGraph = G;
        if (!MacroGraph) { C.Err(FString::Printf(TEXT("%s: standard macro %s not found"), *Id, *MacroName)); return nullptr; }
        UK2Node_MacroInstance* N = NewNode<UK2Node_MacroInstance>(C);
        N->SetMacroGraph(MacroGraph);
        FinishNode(N, Index); return N;
    }
    if (Type == TEXT("spawn")) {
        // SpawnActor: created through the engine's own node spawner (same path as the editor's action menu), then the Class pin
        // gets its default and the node is rebuilt so the class's expose-on-spawn pins appear
        UK2Node_ConstructObjectFromClass* N = SpawnLikeEditor<UK2Node_SpawnActorFromClass>(C, Index);
        if (!N) { C.Err(FString::Printf(TEXT("%s: could not spawn the %s node"), *Id, *Type)); return nullptr; }
        UClass* Cls = LoadClassPath(J->GetStringField(TEXT("class")));
        if (!Cls) { C.Err(FString::Printf(TEXT("%s: %s class not found"), *Id, *Type)); return N; }
        if (UEdGraphPin* ClassPin = N->GetClassPin()) { C.Schema->TrySetDefaultObject(*ClassPin, Cls); }
        else C.Err(FString::Printf(TEXT("%s: no Class pin; pins: %s"), *Id, *PinList(N)));
        N->ReconstructNode();
        return N;
    }
    if (Type == TEXT("break") || Type == TEXT("make")) {
        UScriptStruct* S = LoadStructPath(J->GetStringField(TEXT("struct")));
        if (!S) { C.Err(FString::Printf(TEXT("%s: struct not found"), *Id)); return nullptr; }
        if (Type == TEXT("break")) { UK2Node_BreakStruct* N = NewNode<UK2Node_BreakStruct>(C); N->StructType = S; FinishNode(N, Index); return N; }
        UK2Node_MakeStruct* N = NewNode<UK2Node_MakeStruct>(C); N->StructType = S; FinishNode(N, Index); return N;
    }
    if (Type == TEXT("makearray")) {
        UK2Node_MakeArray* N = NewNode<UK2Node_MakeArray>(C);
        const int32 Count = J->HasField(TEXT("count")) ? (int32)J->GetNumberField(TEXT("count")) : 1;
        N->NumInputs = Count;
        FinishNode(N, Index); return N;
    }
    // A map literal, the same shape as makearray (both are UK2Node_MakeContainer). Needed for
    // the game's lobby API, whose CreateLobby/UpdateLobby/FindLobbies all take an arbitrary
    // TMap<FString, FBodycamLobbyAttribute> of session attributes — a map pin cannot carry an
    // inline default in Blueprints, so without this node there is no way to pass one.
    // Pins are "Key 0"/"Value 0", "Key 1"/... ; the error path prints the real names if that
    // ever changes, because FindPinLoose reports PinList(Node) on a miss.
    if (Type == TEXT("makemap")) {
        UK2Node_MakeMap* N = NewNode<UK2Node_MakeMap>(C);
        const int32 Count = J->HasField(TEXT("count")) ? (int32)J->GetNumberField(TEXT("count")) : 1;
        N->NumInputs = Count;
        FinishNode(N, Index); return N;
    }
    if (Type == TEXT("self")) { UK2Node_Self* N = NewNode<UK2Node_Self>(C); FinishNode(N, Index); return N; }
    if (Type == TEXT("createevent")) {
        // Binds one of THIS Blueprint's custom events to a delegate pin - the "Create Event" node in
        // the editor. The only reason we need it: SendAttributionEvent hands its answer back through
        // FAttributionEventResponse, and without a bound delegate that answer is unreadable.
        //
        // The delegate carries ONE BOOL (DECLARE_DYNAMIC_DELEGATE_OneParam(..., bool, bSuccess)), so
        // the custom event named here must take exactly one bool. It is a permission answer, never
        // data: nothing in the game's API lets an HTTP body reach a Blueprint (docs/autojoin.md #4).
        const FString FuncName = J->GetStringField(TEXT("func"));
        UK2Node_CreateDelegate* N = NewNode<UK2Node_CreateDelegate>(C);
        FinishNode(N, Index);
        // Fill the self pin so the node's scope is this Blueprint; the binding itself happens after
        // the link pass (see FBuildCtx::PendingDelegates).
        if (UEdGraphPin* SelfPin = N->FindPin(UEdGraphSchema_K2::PN_Self, EGPD_Input)) {
            UK2Node_Self* SelfNode = NewNode<UK2Node_Self>(C);
            FinishNode(SelfNode, Index);
            if (UEdGraphPin* Out = SelfNode->FindPin(UEdGraphSchema_K2::PN_Self, EGPD_Output)) {
                Out->MakeLinkTo(SelfPin);
            }
        }
        C.PendingDelegates.Add({N, FName(*FuncName), Id});
        return N;
    }
    if (Type == TEXT("adddelegate") || Type == TEXT("removedelegate")) {
        UClass* Owner = ResolveOwnerClass(C, J); if (!Owner) Owner = SelfClass;
        const FName DelName(*J->GetStringField(TEXT("delegate")));
        FMulticastDelegateProperty* Prop = FindFProperty<FMulticastDelegateProperty>(Owner, DelName);
        if (!Prop) { C.Err(FString::Printf(TEXT("%s: delegate %s not found on %s"), *Id, *DelName.ToString(), *Owner->GetName())); return nullptr; }
        UK2Node_BaseMCDelegate* N = Type == TEXT("adddelegate")
            ? static_cast<UK2Node_BaseMCDelegate*>(NewNode<UK2Node_AddDelegate>(C))
            : static_cast<UK2Node_BaseMCDelegate*>(NewNode<UK2Node_RemoveDelegate>(C));
        N->SetFromProperty(Prop, !J->HasField(TEXT("class")), Prop->GetOwnerClass());
        FinishNode(N, Index); return N;
    }
    if (Type == TEXT("existing")) {
        const FName Name(*J->GetStringField(TEXT("name")));
        for (UEdGraphNode* Existing : C.Graph->Nodes) {
            if (UK2Node_CustomEvent* CE = Cast<UK2Node_CustomEvent>(Existing)) { if (CE->CustomFunctionName == Name) return Existing; continue; }
            if (UK2Node_Event* EV = Cast<UK2Node_Event>(Existing)) { if (EV->EventReference.GetMemberName() == Name) return Existing; continue; }
        }
        C.Err(FString::Printf(TEXT("%s: no existing event node named %s in %s"), *Id, *Name.ToString(), *C.Graph->GetName())); return nullptr;
    }
    if (Type == TEXT("functionresult")) {
        UK2Node_FunctionResult* N = NewNode<UK2Node_FunctionResult>(C);
        N->AllocateDefaultPins();
        N->PostPlacedNewNode();
        N->NodePosX = 420 * (Index % 7); N->NodePosY = 260 * (Index / 7);
        return N;
    }
    if (Type == TEXT("entry") || Type == TEXT("result")) {
        // function terminators: reuse the graph's entry/result node (created with the function graph); a user function
        // has no result node until we add one. "params" adds user-defined pins (inputs on entry, outputs on result).
        UEdGraphNode* Found = nullptr;
        UK2Node_FunctionEntry* Entry = nullptr;
        for (UEdGraphNode* Existing : C.Graph->Nodes) {
            if (UK2Node_FunctionEntry* E = Cast<UK2Node_FunctionEntry>(Existing)) Entry = E;
            if (Type == TEXT("entry") && Existing->IsA<UK2Node_FunctionEntry>()) Found = Existing;
            if (Type == TEXT("result") && Existing->IsA<UK2Node_FunctionResult>()) Found = Existing;
        }
        if (!Found && Type == TEXT("result") && Entry) {
            Found = FBlueprintEditorUtils::FindOrCreateFunctionResultNode(Entry);   // what the editor's "add output" does
        }
        if (!Found) { C.Err(FString::Printf(TEXT("%s: no %s node in graph %s"), *Id, *Type, *C.Graph->GetName())); return nullptr; }
        const TArray<TSharedPtr<FJsonValue>>* Params;
        if (J->TryGetArrayField(TEXT("params"), Params)) {
            UK2Node_EditablePinBase* EP = Cast<UK2Node_EditablePinBase>(Found);
            for (const TSharedPtr<FJsonValue>& PV : *Params) {
                const TSharedPtr<FJsonObject>& PJ = PV->AsObject();
                const FName PinName(*PJ->GetStringField(TEXT("name")));
                if (Found->FindPin(PinName)) continue;   // already there (second run on the same graph)
                UEdGraphPin* NewPin = EP ? EP->CreateUserDefinedPin(PinName, PinTypeFromJson(PJ), Type == TEXT("entry") ? EGPD_Output : EGPD_Input) : nullptr;
                if (!NewPin) C.Err(FString::Printf(TEXT("%s: could not add pin %s"), *Id, *PinName.ToString()));
            }
        }
        return Found;
    }
    C.Err(FString::Printf(TEXT("%s: unknown node type '%s'"), *Id, *Type));
    return nullptr;
}

void ApplyDefaults(FBuildCtx& C, UEdGraphNode* Node, const FString& Id, const TSharedPtr<FJsonObject>& Defaults) {
    for (const auto& KV : Defaults->Values) {
        UEdGraphPin* Pin = FindPinLoose(Node, KV.Key, EGPD_Input);
        if (!Pin) { C.Err(FString::Printf(TEXT("%s: no input pin '%s' for default; pins: %s"), *Id, *KV.Key, *PinList(Node))); continue; }
        const FString Value = KV.Value->Type == EJson::String ? KV.Value->AsString() : (KV.Value->Type == EJson::Boolean ? (KV.Value->AsBool() ? TEXT("true") : TEXT("false")) : FString::SanitizeFloat(KV.Value->AsNumber()));
        if (Pin->PinType.PinCategory == UEdGraphSchema_K2::PC_Object || Pin->PinType.PinCategory == UEdGraphSchema_K2::PC_Class ||
            Pin->PinType.PinCategory == UEdGraphSchema_K2::PC_SoftObject || Pin->PinType.PinCategory == UEdGraphSchema_K2::PC_SoftClass) {
            UObject* Obj = LoadPath(Value);
            if (!Obj) { C.Err(FString::Printf(TEXT("%s: default object not found: %s"), *Id, *Value)); continue; }
            C.Schema->TrySetDefaultObject(*Pin, Obj);
            if (Pin->DefaultObject != Obj) C.Err(FString::Printf(TEXT("%s: default object %s rejected for pin %s"), *Id, *Value, *KV.Key));
        } else if (Pin->PinType.PinCategory == UEdGraphSchema_K2::PC_Text) {
            C.Schema->TrySetDefaultText(*Pin, FText::FromString(Value));
        } else {
            C.Schema->TrySetDefaultValue(*Pin, Value);
            const FString Problem = C.Schema->IsPinDefaultValid(Pin, Pin->DefaultValue, Pin->DefaultObject, Pin->DefaultTextValue);
            const FString Note = FString::Printf(TEXT("%s: default '%s' for pin %s (%s/%s) -> stored '%s' %s"), *Id, *Value, *KV.Key, *Pin->PinType.PinCategory.ToString(),
                                                 Pin->PinType.PinSubCategoryObject.IsValid() ? *Pin->PinType.PinSubCategoryObject->GetName() : *Pin->PinType.PinSubCategory.ToString(),
                                                 *Pin->DefaultValue, *Problem);
            if (!Problem.IsEmpty()) C.Err(Note); else if (Pin->DefaultValue != Value) C.Info(TEXT("note: ") + Note);
        }
    }
}

} // namespace

FString UBodycamMirrorTools::BuildGraph(UBlueprint* Blueprint, const FString& GraphName, const FString& JsonText) {
    if (!Blueprint) return TEXT("ERROR: null blueprint");
    FBuildCtx C; C.BP = Blueprint; C.Schema = GetDefault<UEdGraphSchema_K2>();
    C.Graph = FindGraph(Blueprint, GraphName, true);
    if (!C.Graph) return TEXT("ERROR: no graph ") + GraphName;
    TSharedPtr<FJsonObject> Root;
    TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(JsonText);
    if (!FJsonSerializer::Deserialize(Reader, Root) || !Root.IsValid()) return TEXT("ERROR: invalid JSON");

    const TArray<TSharedPtr<FJsonValue>>* NodesArr = nullptr;
    if (Root->TryGetArrayField(TEXT("nodes"), NodesArr)) {
        int32 Index = C.Graph->Nodes.Num();
        for (const TSharedPtr<FJsonValue>& V : *NodesArr) {
            const TSharedPtr<FJsonObject>& J = V->AsObject();
            const FString Id = J->GetStringField(TEXT("id"));
            UEdGraphNode* Node = MakeNode(C, J, Index++);
            if (!Node) continue;
            C.Nodes.Add(Id, Node);
            const TSharedPtr<FJsonObject>* Defaults;
            if (J->TryGetObjectField(TEXT("defaults"), Defaults)) ApplyDefaults(C, Node, Id, *Defaults);
        }
    }
    const TArray<TSharedPtr<FJsonValue>>* Links = nullptr;
    int32 Linked = 0;
    if (Root->TryGetArrayField(TEXT("links"), Links)) {
        for (const TSharedPtr<FJsonValue>& V : *Links) {
            const TArray<TSharedPtr<FJsonValue>>& Pair = V->AsArray();
            if (Pair.Num() != 2) { C.Err(TEXT("link entries must be [\"node.pin\", \"node.pin\"]")); continue; }
            FString A = Pair[0]->AsString(), B = Pair[1]->AsString();
            FString AN, AP, BN, BP;
            if (!A.Split(TEXT("."), &AN, &AP) || !B.Split(TEXT("."), &BN, &BP)) { C.Err(FString::Printf(TEXT("bad link %s -> %s"), *A, *B)); continue; }
            UEdGraphNode** NA = C.Nodes.Find(AN); UEdGraphNode** NB = C.Nodes.Find(BN);
            if (!NA || !NB) { C.Err(FString::Printf(TEXT("link %s -> %s: unknown node"), *A, *B)); continue; }
            UEdGraphPin* PA = FindPinLoose(*NA, AP, EGPD_Output); UEdGraphPin* PB = FindPinLoose(*NB, BP, EGPD_Input);
            if (!PA) { C.Err(FString::Printf(TEXT("link %s -> %s: no output pin '%s' on %s; pins: %s"), *A, *B, *AP, *AN, *PinList(*NA))); continue; }
            if (!PB) { C.Err(FString::Printf(TEXT("link %s -> %s: no input pin '%s' on %s; pins: %s"), *A, *B, *BP, *BN, *PinList(*NB))); continue; }
            const FPinConnectionResponse R = C.Schema->CanCreateConnection(PA, PB);
            if (!C.Schema->TryCreateConnection(PA, PB)) { C.Err(FString::Printf(TEXT("link %s -> %s refused: %s"), *A, *B, *R.Message.ToString())); continue; }
            Linked++;
        }
    }
    // Now that the delegate pins are connected and therefore have a signature, bind the events.
    for (const FBuildCtx::FPendingDelegate& PD : C.PendingDelegates) {
        PD.Node->SetFunction(PD.Func);
        PD.Node->HandleAnyChange(true);
        if (PD.Node->GetFunctionName() != PD.Func) {
            C.Err(FString::Printf(TEXT("%s: CreateEvent could not bind %s - is there a custom event of "
                                       "that name in this Blueprint, with parameters matching the "
                                       "delegate it feeds, and is its delegate pin connected?"),
                                  *PD.Id, *PD.Func.ToString()));
        } else {
            C.Info(FString::Printf(TEXT("%s: CreateEvent bound to %s"), *PD.Id, *PD.Func.ToString()));
        }
    }
    FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(Blueprint);
    FString Out = FString::Printf(TEXT("graph %s: %d node(s), %d link(s), %d error(s)\n"), *C.Graph->GetName(), C.Nodes.Num(), Linked, C.Errors);
    for (const FString& L : C.Log) Out += L + TEXT("\n");
    return Out;
}

FString UBodycamMirrorTools::OverrideFunction(UBlueprint* Blueprint, const FString& FunctionName) {
    if (!Blueprint || !Blueprint->ParentClass) return TEXT("ERROR: null");
    const FName Name(*FunctionName);
    for (UEdGraph* G : Blueprint->FunctionGraphs) if (G && G->GetFName() == Name) return TEXT("exists");
    UFunction* Func = Blueprint->ParentClass->FindFunctionByName(Name);
    if (!Func) return FString::Printf(TEXT("ERROR: parent function %s not found"), *FunctionName);
    UEdGraph* NewGraph = FBlueprintEditorUtils::CreateNewGraph(Blueprint, Name, UEdGraph::StaticClass(), UEdGraphSchema_K2::StaticClass());
    // The UClass overload is what the editor's "Override function" uses: entry/result pins come from the parent's signature.
    // (The UFunction overload copies the parameters as *user-defined* pins, which then clash with the inherited ones.)
    FBlueprintEditorUtils::AddFunctionGraph<UClass>(Blueprint, NewGraph, /*bIsUserCreated*/ false, Func->GetOwnerClass());
    FString Pins;
    for (UEdGraphNode* N : NewGraph->Nodes) Pins += N->GetClass()->GetName() + TEXT("[") + PinList(N) + TEXT("] ");
    return TEXT("ok: ") + Pins;
}

bool UBodycamMirrorTools::SetVariableReplicated(UBlueprint* Blueprint, const FString& VariableName, bool bReplicated, const FString& RepNotifyFunction) {
    if (!Blueprint) return false;
    const FName Name(*VariableName);
    for (FBPVariableDescription& Var : Blueprint->NewVariables) {
        if (Var.VarName == Name) {
            if (bReplicated) Var.PropertyFlags |= CPF_Net; else Var.PropertyFlags &= ~(CPF_Net | CPF_RepNotify);
            if (bReplicated && !RepNotifyFunction.IsEmpty()) { Var.PropertyFlags |= CPF_RepNotify; Var.RepNotifyFunc = FName(*RepNotifyFunction); }
            FBlueprintEditorUtils::MarkBlueprintAsStructurallyModified(Blueprint);
            return true;
        }
    }
    return false;
}

FString UBodycamMirrorTools::EnsureFunctionGraph(UBlueprint* Blueprint, const FString& FunctionName) {
    UEdGraph* G = FindGraph(Blueprint, FunctionName, true);
    if (!G) return TEXT("ERROR");
    FString Pins;
    for (UEdGraphNode* N : G->Nodes) Pins += N->GetClass()->GetName() + TEXT("[") + PinList(N) + TEXT("] ");
    return TEXT("ok: ") + Pins;
}
