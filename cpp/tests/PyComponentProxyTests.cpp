#include <function/scene/PyComponentProxy.h>

#ifdef NDEBUG
#undef NDEBUG
#endif
#include <cassert>
#include <pybind11/embed.h>

namespace py = pybind11;

int main()
{
    py::scoped_interpreter interpreter{};
    py::exec(R"PY(
class ContractScheduler:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    def count(self):
        self.calls += 1
        return self.value
)PY");

    const py::object schedulerType = py::globals()["ContractScheduler"];
    const py::object scheduler = schedulerType(3);
    assert(infernux::PyComponentProxy::ReadCoroutineSchedulerCount(scheduler) == 3);
    assert(scheduler.attr("calls").cast<int>() == 1);

    scheduler.attr("value") = 0;
    assert(infernux::PyComponentProxy::ReadCoroutineSchedulerCount(scheduler) == 0);
    assert(scheduler.attr("calls").cast<int>() == 2);
    assert(infernux::PyComponentProxy::ReadCoroutineSchedulerCount(py::none()) == 0);
    return 0;
}
